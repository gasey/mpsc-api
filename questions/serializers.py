from rest_framework import serializers
from .models import Question, VerificationResult

class QuestionSerializer(serializers.ModelSerializer):
    answer_letter = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'question_id', 'stem', 'options', 'answer_index', 'answer_letter',
            'answer_source', 'paper_id', 'subject', 'topic', 'has_diagram',
            'diagram_image', 'difficulty', 'created_at'
        ]

    def get_answer_letter(self, obj):
        return chr(65 + obj.answer_index)  # A, B, C, D


class VerificationResultSerializer(serializers.ModelSerializer):
    question = QuestionSerializer(read_only=True)

    class Meta:
        model = VerificationResult
        fields = ['id', 'question', 'status', 'marked_answer', 'model_answer', 'your_decision', 'your_reasoning', 'reviewed_at']
