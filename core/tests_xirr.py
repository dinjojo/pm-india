"""PortfolioView.xirr: flows belong to the owning user only; dividends count."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Account, Asset, Transaction
from core.portfolio import portfolio_view, refresh_holdings

User = get_user_model()


class XirrTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.acct_a = Account.objects.create(user=self.alice, name="A")
        self.acct_b = Account.objects.create(user=self.bob, name="B")
        self.asset = Asset.objects.create(asset_type="mf", name="Fund", identifier="XIRRTST01")
        self.tx_of = lambda acct, kind, d, amt: Transaction.objects.create(
            asset=self.asset, account=acct, kind=kind, date=d,
            quantity=1 if kind == "buy" else 0, price=1, amount=Decimal(amt))

    def test_other_users_transactions_do_not_pollute_xirr(self):
        # Alice invests 1000 and it is still worth 1000 (no price feed → current
        # = invested), so her XIRR is exactly 0%.
        self.tx_of(self.acct_a, "buy", date(2024, 1, 1), 1000)
        # Bob's large outflow must not leak into Alice's return.
        self.tx_of(self.acct_b, "buy", date(2024, 1, 1), 500000)
        refresh_holdings()
        pv = portfolio_view(self.alice)
        self.assertEqual(pv.xirr, 0.0)

    def test_dividend_is_a_positive_flow(self):
        self.tx_of(self.acct_a, "buy", date(2024, 1, 1), 1000)
        self.tx_of(self.acct_a, "dividend", date(2024, 6, 1), 100)
        refresh_holdings()
        pv = portfolio_view(self.alice)
        self.assertGreater(pv.xirr, 0.0)

    def test_no_flows_means_no_xirr(self):
        self.assertIsNone(portfolio_view(self.alice).xirr)
