import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from app import Application


class FilterFeedbackTests(unittest.TestCase):
    def test_zero_result_still_displays_the_examined_and_filtered_communities(self):
        view=NS(stats=Mock(),api_stats=Mock())
        Application.handle_event(view,'stats',{'new':0,'contacts':0,'scanned':0,'seen':17,'skipped':17})
        text=view.stats.set.call_args.args[0]
        self.assertIn('Проверено: 17',text)
        self.assertIn('Пропущено: 17',text)

    def test_finished_run_explains_why_no_contacts_were_checked(self):
        view=NS(status=Mock(),set_running=Mock(),history=NS(total=lambda:39),closing=False)
        result=NS(path=Path('/tmp/contact-test.txt'),cancelled=False,reason='',contacts={},new_contacts={},seen={1,2},skipped=2)
        Application.handle_event(view,'finished',result)
        self.assertIn('отсеяны фильтрами до проверки контактов',view.status.set.call_args.args[0])
