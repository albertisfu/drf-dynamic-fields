"""
Mixin to dynamically select only a subset of fields per DRF resource.
"""

import warnings

from django.conf import settings
from django.db.models import Prefetch
from django.utils.functional import cached_property


class DynamicFieldsMixin(object):
    """
    A serializer mixin that takes an additional `fields` argument that controls
    which fields should be displayed.
    """

    @cached_property
    def fields(self):
        """
        Filters the fields according to the `fields` query parameter.

        A blank `fields` parameter (?fields) will remove all fields. Not
        passing `fields` will pass all fields individual fields are comma
        separated (?fields=id,name,url,email,teachers__age).

        """

        fields = super(DynamicFieldsMixin, self).fields
        if not hasattr(self, "_context"):
            # We are being called before a request cycle
            return fields

        # Only filter if this is the root serializer, or if the parent is the
        # root serializer with many=True
        is_root = self.root == self
        parent_is_list_root = self.parent == self.root and getattr(
            self.parent, "many", False
        )
        if not (is_root or parent_is_list_root):
            return fields

        try:
            request = self.context["request"]
        except KeyError:
            conf = getattr(settings, "DRF_DYNAMIC_FIELDS", {})
            if not conf.get("SUPPRESS_CONTEXT_WARNING", False) is True:
                warnings.warn(
                    "Context does not have access to request. "
                    "See README for more information."
                )
            return fields

        # NOTE: drf test framework builds a request object where the query
        # parameters are found under the GET attribute.
        params = getattr(request, "query_params", getattr(request, "GET", None))
        if params is None:
            warnings.warn("Request object does not contain query parameters")

        try:
            filter_fields = params.get("fields", None).split(",")
        except AttributeError:
            filter_fields = []

        try:
            omit_fields = params.get("omit", None).split(",")
        except AttributeError:
            omit_fields = []

        self._flat_allow = set()
        self._flat_omit = set()
        self._nested_allow = {}
        self._nested_omit = {}

        # store top-level and nested fields specified in the `fields` argument.
        for filtered_field in filter_fields:
            if "__" in filtered_field:
                parent, child = filtered_field.split("__", 1)
                self._nested_allow.setdefault(parent, []).append(child)
                # If a nested field is allowed the related parent level field
                # must also be allowed
                self._flat_allow.add(parent)
            else:
                self._flat_allow.add(filtered_field)

        # store top-level and nested fields in the `omit` argument.
        for omitted_field in omit_fields:
            if "__" in omitted_field:
                parent, child = omitted_field.split("__", 1)
                self._nested_omit.setdefault(parent, []).append(child)
            else:
                self._flat_omit.add(omitted_field)

        # Drop top-level fields
        existing = set(fields.keys())
        if "fields" in params:
            allowed = self._flat_allow
        else:
            allowed = existing
        omitted = self._flat_omit

        for field in existing:
            if field not in allowed:
                fields.pop(field, None)
            if field in omitted:
                fields.pop(field, None)

        # Drop omitted child fields from nested serializers
        for parent, omit_list in self._nested_omit.items():
            field = fields[parent]
            nested_serializer = getattr(field, "child", field)
            if hasattr(nested_serializer, "fields"):
                for child in omit_list:
                    nested_serializer.fields.pop(child, None)

        # Drop non-allowed child fields from the nested serializers
        for parent, allow_list in self._nested_allow.items():
            field = fields[parent]
            nested_serializer = getattr(field, "child", field)
            if hasattr(nested_serializer, "fields"):
                for child_name in list(nested_serializer.fields):
                    if child_name not in allow_list:
                        nested_serializer.fields.pop(child_name, None)

        return fields

    def _get_disallowed_top_level_fields_to_defer(self):
        """
        Determine which top-level model fields should be deferred when an explicit
        fields filter is in use.
        Other model fields not explicitly included in 'fields' are deferred.
        """
        allow = getattr(self, "_flat_allow", None)
        model = getattr(self.Meta, "model", None)
        if not allow or model is None:
            return []

        # Filter out fields that have a database column associated with them.
        field_names = [
            field.name
            for field in model._meta.get_fields()
            if getattr(field, "concrete", False)
        ]
        return [field_name for field_name in field_names if field_name not in allow]

    def _get_disallowed_nested_level_fields_to_defer(self):
        """
        Determine which nested-model fields should be deferred for each parent serializer
        when an explicit fields filter is in use.
        Other model nested fields not explicitly included in 'fields' are deferred.
        """
        fields_to_defer = []
        for parent, allow_list in getattr(self, "_nested_allow", {}).items():
            field = self.fields.get(parent)
            if not field:
                continue

            child_serializer = getattr(field, "child", field)
            nested_model = getattr(child_serializer.Meta, "model", None)
            if nested_model is None:
                continue

            # Filter out nested fields that have a database column associated
            # with them.
            field_names = [
                field.name
                for field in nested_model._meta.get_fields()
                if getattr(field, "concrete", False)
            ]
            for field_name in field_names:
                if field_name not in allow_list:
                    fields_to_defer.append(f"{parent}__{field_name}")

        return fields_to_defer

    def get_deferred_model_fields(self):
        """
        Returns a flat list of omitted model-fields; top-level and nested.
        Ensures that parsing of "fields"/"omit" has run by accessing ".fields".
        """

        # Trigger parsing of required attributes if not already set
        if not all(
            hasattr(self, attr)
            for attr in ("_flat_omit", "_nested_omit", "_flat_allow", "_nested_allow")
        ):
            _ = self.fields

        flat_omit = getattr(self, "_flat_omit", [])
        nested_omit = getattr(self, "_nested_omit", {})
        deferred = []
        # Set omit top-level fields to defer
        deferred.extend(flat_omit)

        # Set omit nested-level fields to defer
        deferred.extend(
            f"{parent}__{child}"
            for parent, children in nested_omit.items()
            for child in children
        )
        # Set disallowed top-level fields to defer
        deferred.extend(self._get_disallowed_top_level_fields_to_defer())
        # Set disallowed nested-level fields to defer
        deferred.extend(self._get_disallowed_nested_level_fields_to_defer())

        # Remove any duplicate fields
        return list(set(deferred))


class DeferredFieldsMixin:
    """ViewSet Mixin that:
    - defers top‐level model columns based on omit/fields
    - builds a Prefetch for each nested relation to defer its columns too
    """

    @staticmethod
    def _split_deferred_fields(fields):
        """Split deferred fields into top‐level fields and nested relations."""
        parent = []
        nested = {}
        for field in fields:
            if "__" in field:
                parent_field, child_field = field.split("__", 1)
                nested.setdefault(parent_field, []).append(child_field)
            else:
                parent.append(field)
        return parent, nested

    @staticmethod
    def _apply_nested_prefetch(qs, nested_map, serializer):
        """For each nested relation, add a Prefetch that defers its specified
        fields.
        """
        for parent_field, child_fields in nested_map.items():
            field = serializer.fields.get(parent_field)
            if not field:
                continue

            child_serializer = getattr(field, "child", field)
            model = getattr(child_serializer.Meta, "model", None)
            if not model:
                continue

            qs = qs.prefetch_related(
                Prefetch(parent_field, queryset=model.objects.defer(*child_fields))
            )
        return qs

    def get_queryset(self):
        """
        Returns a queryset with top-level and nested fields deferred to
        optimize database retrieval.
        """
        qs = super().get_queryset()
        # instantiate serializer so deferred fields are calculated
        serializer = self.get_serializer_class()(context=self.get_serializer_context())

        # split deferred fields into top-level and nested
        fields = serializer.get_deferred_model_fields()
        parent_fields, nested_map = self._split_deferred_fields(fields)

        # defer top-level fields
        if parent_fields:
            qs = qs.defer(*parent_fields)

        # defer nested fields via Prefetch
        qs = self._apply_nested_prefetch(qs, nested_map, serializer)
        return qs
