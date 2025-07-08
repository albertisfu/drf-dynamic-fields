from rest_framework.viewsets import ModelViewSet
from rest_framework.response import Response
from rest_framework import status

from .models import School, Teacher, ParentMany
from .serializers import SchoolSerializer, TeacherSerializer, ParentManySerializer
from drf_dynamic_fields import DeferredFieldsMixin


class TeacherViewSet(ModelViewSet):
    queryset = Teacher.objects.all()
    serializer_class = TeacherSerializer


class SchoolViewSet(ModelViewSet):
    queryset = School.objects.all()
    serializer_class = SchoolSerializer


class SchoolDeferredViewSet(DeferredFieldsMixin, ModelViewSet):
    serializer_class = SchoolSerializer
    queryset = School.objects.all()

    def list(self, request):
        qs = self.get_queryset()
        prefetch_names = [
            getattr(p, "lookup", None) or getattr(p, "prefetch_to", None)
            for p in qs._prefetch_args
        ]

        return Response(
            {"deferred": qs._deferred_args, "prefetches": prefetch_names},
            status=status.HTTP_200_OK,
        )

class ParentManyDeferredViewSet(DeferredFieldsMixin, ModelViewSet):
    serializer_class = ParentManySerializer
    queryset = ParentMany.objects.select_related(
            "grant_parent",
        ).prefetch_related(
            "child",
        ).order_by("-id")

    def list(self, request):
        qs = self.get_queryset()
        deferred_fields, _ = qs.query.deferred_loading
        return Response(
            {"deferred": deferred_fields, "prefetches": qs._prefetch_related_lookups},
            status=status.HTTP_200_OK,
        )