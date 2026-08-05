from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import QuestionViewSet, VerificationViewSet

router = DefaultRouter()
router.register(r'questions', QuestionViewSet, basename='question')
router.register(r'verification', VerificationViewSet, basename='verification')

urlpatterns = [
    path('', include(router.urls)),
]
