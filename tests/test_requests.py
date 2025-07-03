#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
test_drf-dynamic-fields
------------

Test for the full request cycle using dynamic fields mixns
"""
from collections import OrderedDict
from unittest.mock import MagicMock

from django.db.models import Prefetch
from django.test import TestCase, RequestFactory

from rest_framework.reverse import reverse

from .serializers import SchoolSerializer, TeacherSerializer
from .models import Teacher, School
from .views import SchoolDeferredViewSet


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

    @staticmethod
    def make_qs_mock():
        """
        Mock that records .defer() and .prefetch_related() calls for testing.
        """
        qs_mock = MagicMock()
        qs_mock._deferred_args = []
        qs_mock._prefetch_args = []

        def mock_defer(*args):
            qs_mock._deferred_args.extend(args)
            return qs_mock

        qs_mock.defer.side_effect = mock_defer

        def mock_prefetch(*lookups):
            for lookup in lookups:
                if isinstance(lookup, Prefetch):
                    qs_mock._prefetch_args.append(lookup)
            return qs_mock

        qs_mock.prefetch_related.side_effect = mock_prefetch

        return qs_mock

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
        self.assertEqual(response.data["prefetches"], ["teachers"])
        self.assertEqual(len(qs_mock._prefetch_args), 1)

        # Confirm the prefetch deferred field matches.
        prefetch = qs_mock._prefetch_args[0]
        deferred_fields, _ = prefetch.queryset.query.deferred_loading
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
        self.assertEqual(response.data["prefetches"], ["teachers"])
        self.assertEqual(len(qs_mock._prefetch_args), 1)

        # Confirm the prefetch deferred field matches the no selected fields.
        prefetch = qs_mock._prefetch_args[0]
        deferred_fields, _ = prefetch.queryset.query.deferred_loading
        self.assertEqual(deferred_fields, {"age", "id"})
