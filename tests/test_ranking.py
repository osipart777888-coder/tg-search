import unittest
from find_top_legal import legal_relevant, ranked_rows


class RankingTests(unittest.TestCase):
    def test_top_ranks_by_live_audience_not_search_order(self):
        rows = [{"username": "small", "participants": 2000},
                {"username": "largest", "participants": 50000},
                {"username": "medium", "participants": 9000}]
        self.assertEqual([r["username"] for r in ranked_rows(rows, 2)], ["largest", "medium"])

    def test_unknown_and_too_small_audience_excluded(self):
        rows = [{"username": "unknown"}, {"username": "tiny", "participants": 999},
                {"username": "valid", "participants": 1000}]
        self.assertEqual([r["username"] for r in ranked_rows(rows)], ["valid"])

    def test_relevance(self):
        self.assertTrue(legal_relevant("Юристы России", "Обсуждение судебной практики"))
        self.assertTrue(legal_relevant("Адвокат Иванов", "Консультации"))
        self.assertFalse(legal_relevant("Продажа автомобилей", "Самые выгодные предложения"))
        self.assertFalse(legal_relevant("Православный канал", "Проповеди"))
        self.assertFalse(legal_relevant("Арбитраж трафика", "Реклама и таргетинг"))
        self.assertFalse(legal_relevant("Арбитражник с авито", "Партнер криптовалютной биржи"))

    def test_reviewed_nonlegal_channels_do_not_rank(self):
        rows = [{"username": "advocatEgorov", "participants": 100000},
                {"username": "legallawyer", "participants": 5000}]
        self.assertEqual([r['username'] for r in ranked_rows(rows)], ['legallawyer'])
