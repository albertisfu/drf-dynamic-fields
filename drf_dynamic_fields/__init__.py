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
        separated (?fields=id,name,url,email).

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

        # Save for deferred logic
        self._flat_allow = set()
        self._flat_omit = set()
        self._nested_allow = {}
        self._nested_omit = {}

        for filtered_field in filter_fields:
            if "__" in filtered_field:
                parent, child = filtered_field.split("__", 1)
                self._nested_allow.setdefault(parent, []).append(child)
                # If a nested field is allowed the related parent level field
                # must also be allowed
                self._flat_allow.add(parent)
            else:
                self._flat_allow.add(filtered_field)

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

        return fields

    def to_representation(self, instance):
        """Use this method to prune filtered fields from a nested serializer."""
        representation = super(DynamicFieldsMixin, self).to_representation(instance)

        # Apply nested omit on dicts and lists of dicts
        for parent, omit_list in getattr(self, "_nested_omit", {}).items():
            if parent not in representation:
                continue
            parent_instance = representation[parent]

            # helper to drop keys on a single dict
            def do_omit(d):
                for child in omit_list:
                    d.pop(child, None)

            if isinstance(parent_instance, dict):
                do_omit(parent_instance)
            elif isinstance(parent_instance, list):
                for item in parent_instance:
                    if isinstance(item, dict):
                        do_omit(item)

        # Apply nested allow on dicts and lists of dicts
        for parent, allow_list in getattr(self, "_nested_allow", {}).items():
            if parent not in representation:
                continue

            parent_instance = representation[parent]

            def do_allow(d):
                return {
                    field_name: field_value
                    for field_name, field_value in d.items()
                    if field_name in allow_list
                }

            if isinstance(parent_instance, dict):
                representation[parent] = do_allow(parent_instance)
            elif isinstance(parent_instance, list):
                representation[parent] = [
                    do_allow(item) if isinstance(item, dict) else item
                    for item in parent_instance
                ]

        return representation

    def _flat_whitelist_deferred(self):
        """
        Determine which top-level model fields should be deferred when an explicit
        omit/fields filter is in use.
        """
        allow = getattr(self, "_flat_allow", None)
        model = getattr(self.Meta, "model", None)
        if not allow or model is None:
            return []

        names = [
            fld.name
            for fld in model._meta.get_fields()
            if getattr(fld, "concrete", False)
        ]
        return [
            name
            for name in names
            if name not in allow and name not in getattr(self, "_flat_omit", [])
        ]

    def _nested_whitelist_deferred(self):
        """
        Determine which nested-model fields should be deferred for each parent serializer
        when an explicit fields/omit filter is in use.
        """
        results = []
        for parent, allow_list in getattr(self, "_nested_allow", {}).items():
            field = self.fields.get(parent)
            if not field:
                continue

            child_ser = getattr(field, "child", field)
            nested_model = getattr(child_ser.Meta, "model", None)
            if nested_model is None:
                continue

            omitted = set(getattr(self, "_nested_omit", {}).get(parent, []))
            concrete_names = [
                fld.name
                for fld in nested_model._meta.get_fields()
                if getattr(fld, "concrete", False)
            ]
            for name in concrete_names:
                if name not in allow_list and name not in omitted:
                    results.append(f"{parent}__{name}")

        return results

    def get_deferred_model_fields(self):
        """
        Returns flat list of omitted model-fields; top-level and nested.
        Ensures that parsing of "fields"/"omit" has run by accessing ".fields".
        """

        # Trigger parsing of _flat_omit and _nested_omit if not already set
        if not hasattr(self, "_flat_omit") or not hasattr(self, "_nested_omit"):
            _ = self.fields  # trigger parsing

        flat_omit = getattr(self, "_flat_omit", [])
        nested_omit = getattr(self, "_nested_omit", {})

        deferred = []
        # top-level
        deferred.extend(flat_omit)
        # nested
        deferred.extend(
            f"{parent}__{child}"
            for parent, children in nested_omit.items()
            for child in children
        )

        deferred.extend(self._flat_whitelist_deferred())
        deferred.extend(self._nested_whitelist_deferred())

        # dedupe and preserve order
        return list(dict.fromkeys(deferred))


class DeferredFieldsMixin:
    """ViewSet Mixin that:
    - defers top‐level model columns based on omit/fields
    - builds a Prefetch for each nested relation to defer its columns too
    """

    @staticmethod
    def _split_deferred_fields(fields):
        """Split deferred fields into top‐level fields and nested relations."""
        parent, nested = [], {}
        for field in fields:
            if "__" in field:
                rel, fld = field.split("__", 1)
                nested.setdefault(rel, []).append(fld)
            else:
                parent.append(field)
        return parent, nested

    @staticmethod
    def _apply_nested_prefetch(qs, nested_map, serializer):
        """For each nested relation, add a Prefetch that defers its specified
        fields.
        """
        for rel, child_fields in nested_map.items():
            field = serializer.fields.get(rel)
            if not field:
                continue

            child_ser = getattr(field, "child", field)
            model = getattr(child_ser.Meta, "model", None)
            if not model:
                continue

            qs = qs.prefetch_related(
                Prefetch(rel, queryset=model.objects.defer(*child_fields))
            )
        return qs

    def get_queryset(self):
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
