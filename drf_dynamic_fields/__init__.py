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
            if parent not in fields:
                continue
            field = fields[parent]
            nested_serializer = getattr(field, "child", field)
            if hasattr(nested_serializer, "fields"):
                for child in omit_list:
                    nested_serializer.fields.pop(child, None)

        # Drop non-allowed child fields from the nested serializers
        for parent, allow_list in self._nested_allow.items():
            if parent not in fields or not allow_list:
                continue
            field = fields[parent]
            nested_serializer = getattr(field, "child", field)
            if hasattr(nested_serializer, "fields"):
                for child_name in list(nested_serializer.fields):
                    if child_name not in allow_list:
                        nested_serializer.fields.pop(child_name, None)

        return fields

    def _filter_top_level_fields_to_defer(self, field_list, keep_if):
        """Method to retrieve the top-level fields to defer, given a list of
        field names (allow or omit) and a condition that determines which
        fields to defer.
        """
        model = getattr(self.Meta, "model", None)
        if not field_list or model is None:
            return []

        all_fields = [
            f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)
        ]
        return [name for name in all_fields if keep_if(name, field_list)]

    def _get_disallowed_top_level_fields_to_defer(self):
        """Determine which top-level model fields should be deferred when an
        explicit fields filter is in use.
        Other model fields not explicitly included in 'fields' are deferred.
        """
        allow = getattr(self, "_flat_allow", None)
        return self._filter_top_level_fields_to_defer(
            allow, keep_if=lambda name, allow: name not in allow
        )

    def _get_omit_top_level_fields_to_defer(self):
        """
        Determine which top-level model fields should be deferred when an
        explicit omit filter is in use. Valid database fields in the omit list
        will be deferred.
        """
        omit = getattr(self, "_flat_omit", None)
        return self._filter_top_level_fields_to_defer(
            omit, keep_if=lambda name, omit: name in omit
        )

    def _get_nested_level_fields_to_defer(self, nested_mapping, should_defer):
        """Method to retrieve nested fields to defer based on a mapping and a
        defer condition.
        """
        fields_to_defer = []
        for parent, items in nested_mapping.items():
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
                f.name
                for f in nested_model._meta.get_fields()
                if getattr(f, "concrete", False)
            ]

            # Determine which nested fields to defer
            for name in field_names:
                if should_defer(name, items):
                    fields_to_defer.append(f"{parent}__{name}")

        return fields_to_defer

    def _get_disallowed_nested_level_fields_to_defer(self):
        """Determine which top-level model fields should be deferred when an
         explicit fields filter is in use.
        Other model fields not explicitly included in 'fields' are deferred.
        """
        allow_map = getattr(self, "_nested_allow", {})
        return self._get_nested_level_fields_to_defer(
            allow_map,
            should_defer=lambda name, allow_list: name not in allow_list,
        )

    def _get_omit_nested_level_fields_to_defer(self):
        """
        Determine which nested-model fields should be deferred for each nested
        serializer when an explicit omit filter is in use.
        """
        omit_map = getattr(self, "_nested_omit", {})
        return self._get_nested_level_fields_to_defer(
            omit_map, should_defer=lambda name, omit_list: name in omit_list
        )

    def get_deferred_model_fields(self):
        """
        Returns a flat list of omitted model-fields; top-level and nested.
        Ensures that parsing of "fields"/"omit" has run by accessing ".fields".
        """

        # Trigger parsing of required attributes if not already set
        if not all(
            hasattr(self, attr)
            for attr in (
                "_flat_omit",
                "_nested_omit",
                "_flat_allow",
                "_nested_allow",
            )
        ):
            _ = self.fields

        deferred = [
            *self._get_omit_top_level_fields_to_defer(),
            *self._get_omit_nested_level_fields_to_defer(),
            *self._get_disallowed_top_level_fields_to_defer(),
            *self._get_disallowed_nested_level_fields_to_defer(),
        ]
        # Remove any duplicate fields
        return list(set(deferred))


class DeferredFieldsMixin:
    """ViewSet Mixin that:
    - defers top‐level model columns based on omit/fields
    - omit deferring select related fields
    - builds a Prefetch for each nested relation to defer its columns,
    merging cleanly with any existing prefetches in the right order.
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

    def get_queryset(self):
        qs = super().get_queryset()
        # Skip for queries that uses values() or annotate()
        if qs.query.values_select or qs.query.annotations:
            return qs

        existing_select_related = set(qs.query.select_related or ())
        original_only_or_deferred_fields, _ = qs.query.deferred_loading
        serializer = self.get_serializer_class()(context=self.get_serializer_context())
        all_fields = serializer.get_deferred_model_fields()
        parent_fields, nested_map = self._split_deferred_fields(all_fields)

        # Remove select_related fields from top level deferred fields
        parent_defer_to_keep = set(parent_fields) - existing_select_related

        # Only defer top-level fields if neither only() nor defer() was used in
        # the original queryset.
        if not original_only_or_deferred_fields:
            qs = qs.defer(*parent_defer_to_keep)

        # Prepare to rebuild prefetch_related in correct order
        nested_prefetches = []
        existing_simple_lookups = list(qs._prefetch_related_lookups)
        for parent_field, child_fields in nested_map.items():
            field = serializer.fields.get(parent_field)
            if not field:
                continue

            child_serializer = getattr(field, "child", field)
            child_model = getattr(child_serializer.Meta, "model", None)
            parent_model = serializer.Meta.model
            if not child_model or not parent_model:
                continue

            # Look for FKs in the child serializer linked to the parent model.
            # f.many_to_one is True for ForeignKey fields
            # and f.remote_field.model is the parent model.
            fk_names = {
                f.name
                for f in child_model._meta.get_fields()
                if getattr(f, "many_to_one", False)
                and getattr(f.remote_field, "model", None) == parent_model
            }
            child_fields = [f for f in child_fields if f not in fk_names]
            if child_fields:
                nested_prefetches.append(
                    Prefetch(
                        parent_field,
                        queryset=child_model.objects.defer(*child_fields),
                    )
                )

        new_qs = qs._clone()
        # Apply prefetches in the correct order to prevent conflicts.
        new_qs._prefetch_related_lookups = nested_prefetches + existing_simple_lookups
        return new_qs
