from rest_framework import serializers
from .models import Question, VerificationResult, Paper

class BankPaperSerializer(serializers.ModelSerializer):
    examType = serializers.CharField(source='exam_type')
    examName = serializers.CharField(source='exam_name')
    paperNumber = serializers.CharField(source='paper_number')
    paperSubject = serializers.CharField(source='paper_subject')
    sourceFile = serializers.CharField(source='source_file')

    class Meta:
        model = Paper
        fields = ['id', 'examType', 'examName', 'post', 'paperNumber', 'paperSubject', 'year', 'sourceFile']


class BankQuestionSerializer(serializers.ModelSerializer):
    question = serializers.CharField(source='stem')
    answerIndex = serializers.IntegerField(source='answer_index')
    answerSource = serializers.CharField(source='answer_source')
    paperId = serializers.CharField(source='paper_id')
    topicLabel = serializers.CharField(source='topic')
    explanation = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'subject', 'topic', 'topicLabel', 'difficulty', 'question',
            'explanation', 'source', 'options', 'answerIndex', 'answerSource',
            'paperId', 'type',
        ]

    def get_explanation(self, obj):
        return ''

    def get_source(self, obj):
        return 'MPSC Old Questions'

    def get_type(self, obj):
        return 'mcq'

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        rep['id'] = instance.question_id
        return rep


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
