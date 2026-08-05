import json
from django.core.management.base import BaseCommand
from questions.models import Question

class Command(BaseCommand):
    help = 'Load MPSC questions from JSON file'

    def add_arguments(self, parser):
        parser.add_argument('json_file', type=str, help='Path to mpsc_bank_converted.json')

    def handle(self, *args, **options):
        json_file = options['json_file']
        
        with open(json_file) as f:
            data = json.load(f)
        
        questions = data.get('questions', [])
        self.stdout.write(f"Loading {len(questions)} questions...")
        
        created = 0
        for q in questions:
            obj, was_created = Question.objects.get_or_create(
                question_id=q.get('id'),
                defaults={
                    'stem': q.get('question', '')[:1000],
                    'options': q.get('options', []),
                    'answer_index': q.get('answerIndex', -1),
                    'answer_source': q.get('answerSource', 'derived'),
                    'paper_id': q.get('paperId', ''),
                    'subject': q.get('subject', 'gk'),
                    'topic': q.get('topic', ''),
                    'has_diagram': bool(q.get('_diagramPath')),
                    'diagram_image': q.get('_diagramPath', ''),
                    'difficulty': q.get('difficulty', 'medium'),
                }
            )
            if was_created:
                created += 1
        
        self.stdout.write(self.style.SUCCESS(f'Created {created} new questions'))
