"""
Mixin to dynamically select only a subset of fields per DRF resource.
"""

import warnings

from collections import defaultdict
from django.conf import settings
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

        # Drop any fields that are not specified in the `fields` argument.
        self._nested_allow = defaultdict(list)
        self._nested_omit = defaultdict(list)
        self._flat_allow = set()
        self._flat_omit = set()

        # store top-level and nested fields specified in the `fields` argument.
        for filtered_field in filter_fields:
            if "__" in filtered_field:
                parent, child = filtered_field.split("__", 1)
                self._nested_allow[parent].append(child)
                # If a nested field is allowed the related parent level field
                # must also be allowed
                self._flat_allow.add(parent)
            else:
                self._flat_allow.add(filtered_field)

        # store top-level and nested fields in the `omit` argument.
        for omitted_field in omit_fields:
            if "__" in omitted_field:
                parent, child = omitted_field.split("__", 1)
                self._nested_omit[parent].append(child)
            else:
                self._flat_omit.add(omitted_field)

        # Drop top-level fields
        existing = set(fields.keys())
        if "fields" in params:
            allowed = self._flat_allow
        else:
            # no fields param given, don't filter.
            allowed = existing
        omitted = self._flat_omit

        for field in existing:

            if field not in allowed:
                fields.pop(field, None)

            if field in omitted:
                fields.pop(field, None)

        # Drop omitted and non-allowed child fields from nested serializers
        self.prune_nested_fields(fields)

        return fields

    @staticmethod
    def _get_nested_serializer(parent, fields):
        """Return the nested serializer for a parent field, or None."""
        field = fields.get(parent)
        if not field:
            return None
        nested = getattr(field, "child", field)
        return nested if hasattr(nested, "fields") else None

    def prune_nested_fields(self, fields):
        """Prune valid nested serializer fields based on the _nested_omit and _nested_allow lists."""
        valid_parents = (
            self._nested_omit.keys() | self._nested_allow.keys()
        ) & fields.keys()
        for parent in valid_parents:
            nested_serializer = self._get_nested_serializer(parent, fields)
            if nested_serializer is None:
                continue

            # Drop omitted child fields from nested serializers
            for name in self._nested_omit.get(parent, ()):
                nested_serializer.fields.pop(name, None)

            # Drop non-allowed child fields from the nested serializers
            allow_list = self._nested_allow.get(parent)
            if allow_list:
                nested_serializer.fields = {
                    name: field
                    for name, field in nested_serializer.fields.items()
                    if name in allow_list
                }

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

    def get_model_fields_to_defer(self):
        """
        Returns a flat list of filtered model-fields; top-level and nested.
        Ensures that parsing of 'fields'/'omit' has run by accessing '.fields'.
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
