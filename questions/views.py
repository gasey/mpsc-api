from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from django.db.models import Q
from django.views.decorators.cache import cache_page
from .models import Question, VerificationResult, Paper
from .serializers import (
    QuestionSerializer, VerificationResultSerializer,
    BankPaperSerializer, BankQuestionSerializer,
)


@cache_page(60 * 60 * 6)
@api_view(['GET'])
def bank(request):
    """Full MPSC question bank in the shape the india-study-map frontend
    expects: {papers: ExamPaper[], questions: BankQuestion[]}. Serializing
    69K+ rows is CPU-heavy on the free instance (~20s cold) — cached for 6h
    so only the first request after a deploy/restart pays that cost."""
    papers = BankPaperSerializer(Paper.objects.all(), many=True).data
    questions = BankQuestionSerializer(Question.objects.all(), many=True).data
    return Response({'papers': papers, 'questions': questions})

class QuestionViewSet(viewsets.ReadOnlyModelViewSet):
    """API for 73,405 MPSC questions"""
    queryset = Question.objects.all()
    serializer_class = QuestionSerializer
    filterset_fields = ['paper_id', 'topic', 'subject', 'difficulty', 'answer_source', 'has_diagram']
    search_fields = ['stem', 'question_id', 'paper_id']
    ordering_fields = ['question_id', 'created_at', 'difficulty']

    @action(detail=False, methods=['get'])
    def with_diagrams(self, request):
        """Get only questions that have diagram images"""
        qs = self.get_queryset().filter(has_diagram=True)
        serializer = self.get_serializer(qs, many=True)
        return Response({'count': qs.count(), 'results': serializer.data})

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get statistics about the question bank"""
        total = Question.objects.count()
        with_diagrams = Question.objects.filter(has_diagram=True).count()
        by_source = {
            'official': Question.objects.filter(answer_source='official').count(),
            'derived': Question.objects.filter(answer_source='derived').count(),
        }
        return Response({
            'total_questions': total,
            'with_diagrams': with_diagrams,
            'by_answer_source': by_source,
        })


class VerificationViewSet(viewsets.ModelViewSet):
    """API for tracking verification of flagged answers"""
    queryset = VerificationResult.objects.all()
    serializer_class = VerificationResultSerializer
    filterset_fields = ['status', 'question__answer_source']

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending verifications"""
        qs = self.get_queryset().filter(status='pending')
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def mark_reviewed(self, request, pk=None):
        """Mark a verification as reviewed with your decision"""
        vr = self.get_object()
        vr.status = 'reviewed'
        vr.your_decision = request.data.get('your_decision')
        vr.your_reasoning = request.data.get('your_reasoning', '')
        vr.save()
        serializer = self.get_serializer(vr)
        return Response(serializer.data)
