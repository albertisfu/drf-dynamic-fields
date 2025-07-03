from rest_framework.viewsets import ModelViewSet
from rest_framework.response import Response
from rest_framework import status

from .models import School, Teacher
from .serializers import SchoolSerializer, TeacherSerializer
from drf_dynamic_fields import DeferredFieldsMixin


class TeacherViewSet(ModelViewSet):
    queryset = Teacher.objects.all()
    serializer_class = TeacherSerializer


class SchoolViewSet(ModelViewSet):
    queryset = School.objects.all()
    serializer_class = SchoolSerializer


class SchoolDeferredViewSet(DeferredFieldsMixin, ModelViewSet):
    serializer_class = SchoolSerializer

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
