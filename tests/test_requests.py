#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
test_drf-dynamic-fields
------------

Test for the full request cycle using dynamic fields mixns
"""
from unittest.mock import MagicMock

import types
from django.test import TestCase, RequestFactory
from django.db.models import Prefetch

from rest_framework.reverse import reverse

from .models import Teacher, School
from .views import SchoolDeferredViewSet, ParentManyDeferredViewSet


class TestDynamicFieldsViews(TestCase):
    """
    Testing using dynamic fields in request framework views.
    """

    def setUp(self):
        """
        Create some teachers and schools.
        """
        teachers = [("Craig", 34), ("Kaz", 29), ("Sun", 62)]

        schools = ["Python Heights High", "Ruby Consolidated", "Java Coffee School"]

        t = [Teacher.objects.create(name=name, age=age) for name, age in teachers]

        for name in schools:
            s = School.objects.create(name=name)
            s.teachers.add(*t)

    def test_teacher_basic(self):

        response = self.client.get(reverse("teacher-list"))
        for teacher in response.data:
            self.assertEqual(teacher.keys(), {"id", "request_info", "age", "name"})

    def test_teacher_fields(self):

        response = self.client.get(reverse("teacher-list"), {"fields": "id,age"})
        for teacher in response.data:
            self.assertEqual(teacher.keys(), {"id", "age"})

    def test_teacher_omit(self):

        response = self.client.get(reverse("teacher-list"), {"omit": "id,age"})
        for teacher in response.data:
            self.assertEqual(teacher.keys(), {"request_info", "name"})

    def test_nested_teacher_fields(self):

        response = self.client.get(reverse("school-list"), {"fields": "name,teachers"})
        for school in response.data:
            self.assertEqual(school.keys(), {"teachers", "name"})
            self.assertEqual(
                school["teachers"][0].keys(), {"id", "request_info", "age", "name"}
            )


class DeferredFieldsMixinTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = SchoolDeferredViewSet.as_view({"get": "list"})
        self.view_parent = ParentManyDeferredViewSet.as_view({"get": "list"})

    @staticmethod
    def make_qs_mock():
        qs = MagicMock()

        qs.query = types.SimpleNamespace(
            values_select=False,
            annotations={},
            select_related=[],
            deferred_loading=([], []),
            deferred_fields=set(),
        )

        qs._deferred_args = []
        def _defer(*fields):
            qs._deferred_args.extend(fields)
            return qs

        qs.defer.side_effect = _defer

        qs._prefetch_related_lookups = []
        qs._clone.return_value = qs

        return qs

    def test_deffer_nested_omitted_fields(self):
        """Nested level fields omitted should be deferred."""

        qs_mock = self.make_qs_mock()
        SchoolDeferredViewSet.queryset = qs_mock

        request = self.factory.get("/", {"omit": "name,teachers__age"})
        response = self.view(request)
        self.assertEqual(response.status_code, 200)

        # top level name field should be deferred
        self.assertEqual(qs_mock._deferred_args, ["name"])

        # One Prefetch on teachers
        prefetches = [
            p for p in qs_mock._prefetch_related_lookups
            if isinstance(p, Prefetch)
        ]
        self.assertEqual(len(prefetches), 1)
        self.assertEqual(prefetches[0].prefetch_to, "teachers")

        # Confirm the prefetch deferred field matches.
        deferred_fields, _ = prefetches[0].queryset.query.deferred_loading
        self.assertIn("age", deferred_fields)

    def test_deffer_nested_no_selected_fields(self):
        """No selected nested level fields should be deferred."""
        qs_mock = self.make_qs_mock()
        SchoolDeferredViewSet.queryset = qs_mock

        request = self.factory.get("/", {"fields": "id,teachers__name"})
        response = self.view(request)
        self.assertEqual(response.status_code, 200)

        # name field should be deferred
        self.assertEqual(qs_mock._deferred_args, ["name"])

        # One Prefetch for teachers
        prefetches = [
            p for p in qs_mock._prefetch_related_lookups
            if isinstance(p, Prefetch)
        ]
        self.assertEqual(len(prefetches), 1)
        self.assertEqual(prefetches[0].prefetch_to, "teachers")

        # Confirm the prefetch deferred field matches the no selected fields.
        deferred_fields, _ = prefetches[0].queryset.query.deferred_loading
        self.assertEqual(deferred_fields, {"age", "id"})

    def test_deffer_nested_fields_combining_fields_and_omit(self):
        """Omit and allowed fields used together are deferred."""
        qs_mock = self.make_qs_mock()
        SchoolDeferredViewSet.queryset = qs_mock

        request = self.factory.get(
            "/",
            {
                "fields": "id,name,teachers__name,teachers__age",
                "omit": "name,teachers__age",
            },
        )
        response = self.view(request)
        self.assertEqual(response.status_code, 200)

        # name field should be deferred
        self.assertEqual(qs_mock._deferred_args, ["name"])

        # One Prefetch for teachers
        prefetches = [
            p for p in qs_mock._prefetch_related_lookups
            if isinstance(p, Prefetch)
        ]
        self.assertEqual(len(prefetches), 1)
        self.assertEqual(prefetches[0].prefetch_to, "teachers")

        # Confirm the prefetch deferred field matches the no selected fields.
        deferred_fields, _ = prefetches[0].queryset.query.deferred_loading
        self.assertEqual(deferred_fields, {"age", "id"})

    def test_deffer_fields_custom_queryset(self):
        """ Confirms that the deferring fields logic works correctly with a custom
        queryset that uses select_related and prefetch_related.
        """
        request = self.factory.get(
            "/",
            {
                "fields": "name,age,child__secret,child__public",
                "omit": "age,child__public",
            },
        )
        response = self.view_parent(request)
        self.assertEqual(response.status_code, 200)

        deferred_fields = response.data["deferred"]
        self.assertEqual(deferred_fields, {"age", "id"})

        prefetches = response.data["prefetches"]
        self.assertEqual(len(prefetches), 2)
        self.assertIn("child", prefetches)
