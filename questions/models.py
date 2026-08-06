from django.db import models

class Paper(models.Model):
    """A real MPSC exam paper — referenced by Question.paper_id."""
    id = models.CharField(max_length=255, primary_key=True)
    exam_type = models.CharField(max_length=50)
    exam_name = models.CharField(max_length=255)
    post = models.CharField(max_length=255, blank=True, null=True)
    paper_number = models.CharField(max_length=50, blank=True, null=True)
    paper_subject = models.CharField(max_length=255)
    year = models.IntegerField(blank=True, null=True)
    source_file = models.CharField(max_length=500, blank=True, null=True)

    def __str__(self):
        return self.id


class Question(models.Model):
    """73,405 MPSC questions extracted from 2,492 exam papers"""
    question_id = models.CharField(max_length=50, unique=True, db_index=True)
    stem = models.TextField()
    options = models.JSONField()  # ["A option", "B option", "C option", "D option"]
    answer_index = models.IntegerField()  # 0=A, 1=B, 2=C, 3=D
    answer_source = models.CharField(
        max_length=20,
        choices=[('official', 'Official MPSC Key'), ('derived', 'AI Extracted')],
        default='derived'
    )

    paper_id = models.CharField(max_length=255, db_index=True)
    subject = models.CharField(max_length=100, default='gk')
    topic = models.CharField(max_length=255, db_index=True)

    has_diagram = models.BooleanField(default=False)
    diagram_image = models.CharField(max_length=500, blank=True, null=True)

    difficulty = models.CharField(
        max_length=20,
        choices=[('easy', 'Easy'), ('medium', 'Medium'), ('hard', 'Hard')],
        default='medium'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['question_id']
        indexes = [
            models.Index(fields=['paper_id', 'topic']),
            models.Index(fields=['answer_source']),
        ]

    def __str__(self):
        return f"{self.question_id}: {self.stem[:60]}..."


class VerificationResult(models.Model):
    """Track manual review of 3,997 flagged answers"""
    question = models.OneToOneField(Question, on_delete=models.CASCADE, related_name='verification')

    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending Review'),
            ('reviewed', 'Reviewed'),
            ('ambiguous', 'Ambiguous'),
        ],
        default='pending',
        db_index=True
    )

    marked_answer = models.CharField(max_length=1)  # A, B, C, D
    model_answer = models.CharField(max_length=1)   # What the AI model said
    your_decision = models.CharField(max_length=1, blank=True, null=True)  # What you decided
    your_reasoning = models.TextField(blank=True)

    reviewed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['status', '-reviewed_at']
        indexes = [
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"Verification: {self.question.question_id} - {self.status}"
