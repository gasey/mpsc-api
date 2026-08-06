from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import QuestionViewSet, VerificationViewSet, bank

router = DefaultRouter()
router.register(r'questions', QuestionViewSet, basename='question')
router.register(r'verification', VerificationViewSet, basename='verification')

urlpatterns = [
    path('bank/', bank, name='bank'),
    path('', include(router.urls)),
]
