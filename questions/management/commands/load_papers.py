import json
from django.core.management.base import BaseCommand
from questions.models import Paper

class Command(BaseCommand):
    help = 'Load MPSC exam papers from JSON file'

    def add_arguments(self, parser):
        parser.add_argument('json_file', type=str, help='Path to mpsc_bank_converted.json')

    def handle(self, *args, **options):
        json_file = options['json_file']

        with open(json_file) as f:
            data = json.load(f)

        papers = data.get('papers', [])
        self.stdout.write(f"Loading {len(papers)} papers...")

        created = 0
        for p in papers:
            year = p.get('year')
            try:
                year = int(year) if year not in (None, '', 'None') else None
            except (TypeError, ValueError):
                year = None
            obj, was_created = Paper.objects.get_or_create(
                id=p.get('id'),
                defaults={
                    'exam_type': p.get('examType') or '',
                    'exam_name': p.get('examName') or '',
                    'post': p.get('post') or None,
                    'paper_number': p.get('paperNumber') if p.get('paperNumber') not in (None, 'None') else None,
                    'paper_subject': p.get('paperSubject') or '',
                    'year': year,
                    'source_file': p.get('sourceFile') or None,
                }
            )
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(f'Created {created} new papers'))
