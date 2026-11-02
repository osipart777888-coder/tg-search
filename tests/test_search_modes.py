import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from app import Application, saved_search_target
from tgsearch.communities import COMMUNITY_SEARCH_LABEL
from tgsearch.extraction import ROLE_FILTERS


def variable(value):
    return NS(get=lambda *args: value)


class SearchModeTests(unittest.TestCase):
    def test_old_settings_migrate_and_explicit_target_wins_over_person_roles(self):
        self.assertEqual(saved_search_target({}), 'communities')
        self.assertEqual(saved_search_target({'roles': COMMUNITY_SEARCH_LABEL}), 'communities')
        self.assertEqual(saved_search_target({'roles': next(iter(ROLE_FILTERS))}), 'contacts')
        self.assertEqual(saved_search_target({'search_target': 'communities', 'roles': next(iter(ROLE_FILTERS))}), 'communities')
        self.assertEqual(saved_search_target({'search_target': 'contacts', 'roles': COMMUNITY_SEARCH_LABEL}), 'contacts')

    def test_title_search_is_independent_of_role_and_disabled_contact_fields(self):
        view = NS(**{k: variable(v) for k, v in {
            'search_target': 'communities', 'roles': next(iter(ROLE_FILTERS)),
            'keywords': 'юрист', 'city': 'Челябинск', 'chat_type': 'Все', 'max_chats': '300',
            'posts': 'не число', 'min_members': '1000', 'admins': True, 'bots': True,
            'sources': '', 'max_new': 'не число', 'max_operations': '297', 'cache_hours': '24',
            'force_refresh': False, 'retention_days': '30', 'limit_mode': 'Остановить и сохранить',
            'russia_only': True}.items()})
        options = Application.read_search_options(view)
        options.validate()
        self.assertTrue(options.communities_only)
        self.assertFalse(options.roles)
        self.assertEqual(options.city, 'Челябинск')
        self.assertTrue(options.russia_only)
        self.assertEqual(options.posts, 0)
        self.assertFalse(options.check_admins)
        self.assertFalse(options.include_bots)

    def test_resume_cannot_silently_change_title_search_into_people_search(self):
        view = NS(running=False, export_busy=False, search_target=variable('communities'),
                  history=NS(unfinished=lambda: {'options': {'communities_only': False}}),
                  status=Mock(), add_log=Mock(), backend=Mock(), contacts={'preserved': True})
        Application.start_search(view, resume=True)
        self.assertEqual(view.contacts, {'preserved': True})
        view.backend.submit.assert_not_called()
        self.assertIn('другому режиму', view.status.set.call_args.args[0])
