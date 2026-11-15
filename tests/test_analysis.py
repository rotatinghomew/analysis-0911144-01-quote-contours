import copy
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError

from quote_contours.analysis import analyse
from quote_contours.annual import account, allocate
from quote_contours.client import Client
from quote_contours.domain import validate
from quote_contours.repository import Repository
from quote_contours.service import make_server


def load_fixture():
    return json.loads(Path('fixtures/demo.json').read_text())


def amounts(result):
    return {row['key']: Decimal(row['amount']) for row in result['composition']}


class AnnualAccountingTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()
        self.data = copy.deepcopy(self.data)
        self.contract = self.data['contracts'][0]
        self.scenario = self.data['scenarios'][0]

    def test_composition_reconciles_to_margin(self):
        result = account(self.contract, self.scenario, self.data)
        parts = amounts(result)
        total = (parts['gross_shipped_revenue'] + parts['provisional_quantity_discount']
                 + parts['returns_reversed'] + parts['discount_true_up'] + parts['cost']
                 + parts['freight'] + parts['provisional_rebate'] + parts['rebate_true_up'])
        self.assertEqual(total, parts['margin'])
        self.assertEqual(total, Decimal(result['margin']))
        self.assertEqual(parts['margin'], Decimal(result['margin']))
        self.assertTrue(all(result['checks'].values()))

    def test_returns_use_original_transaction_terms(self):
        # Return month 3 FX differs; the reversal must still use the original
        # month's FX and discount.
        self.scenario['months'][0]['fx']['USD'] = 1.0
        self.data['positions'][0]['fx_rate'] = 7.5
        result = account(self.contract, self.scenario, self.data)
        order = next(row for row in result['orders'] if row['id'] == 'ship-1')
        self.assertEqual(order['fx_rate'], '7.5')
        self.assertEqual(order['fx_source'], 'recorded fx_rate')
        expected = Decimal('18') * Decimal('30') * Decimal('7.5') * (Decimal(1) - Decimal('0.07'))
        self.assertEqual(Decimal(order['return_reversal']), expected)
        # A higher return-month FX could not have been used: that would change
        # this number to 18*30*1.0*0.93.
        self.assertNotEqual(Decimal(order['return_reversal']), Decimal('18') * 30 * Decimal('1') * Decimal('0.93'))

    def test_rebate_uses_annual_net_units_not_monthly(self):
        result = account(self.contract, self.scenario, self.data)
        self.assertEqual(result['units']['net'], 440)
        self.assertEqual(result['rebate']['rate'], 0.03)
        self.assertEqual(Decimal(result['rebate']['base']), Decimal(result['sales']['net_revenue']))
        self.assertTrue(result['checks']['rebate_uses_net_units'])

    def test_rebate_tier_clawback_on_returns(self):
        # 270 shipped would be tier 250; 240 net falls back to tier 0.
        result = account(self.contract, self.scenario, self.data)
        self.assertEqual(result['units']['shipped'], 470)
        self.assertEqual(Decimal(result['rebate']['true_up']), Decimal('108.4752'))
        self.assertGreater(Decimal(result['rebate']['true_up']), 0)
        self.assertEqual(Decimal(result['rebate']['amount']),
                         Decimal(result['rebate']['provisional_amount']) - Decimal(result['rebate']['true_up']))

    def test_yearly_path_is_not_monthly_independent(self):
        result = account(self.contract, self.scenario, self.data)
        rows = result['path']['rows']
        self.assertEqual(rows[-1]['cumulative_net_units'], 440)
        # Rebate crossings are recorded on the cumulative path, not per month.
        crossings = [row for row in result['path']['threshold_crossings'] if row['clause'] == 'annual_rebate']
        self.assertTrue(crossings)
        self.assertEqual(crossings[0]['threshold'], 250)
        # The final year-end rate is the one used in the exact account.
        self.assertEqual(result['path']['year_end']['rebate_rate'], 0.03)

    def test_freight_floor_is_per_delivery_and_not_refunded(self):
        result = account(self.contract, self.scenario, self.data)
        for order in result['orders']:
            expected = max(Decimal('300'), Decimal(order['shipped_units']) * Decimal('1.2')) * Decimal('1')
            self.assertEqual(Decimal(order['freight']), expected)
        # 4 deliveries, each floored at 300.
        self.assertEqual(Decimal(result['freight']), Decimal('1200'))
        self.assertTrue(all(order['freight_floor_binds'] for order in result['orders']))

    def test_discount_tiers_are_not_smoothed(self):
        # A return that drops a delivery below the 100-unit tier causes a
        # discrete true-up, not a smooth average discount.
        contract = copy.deepcopy(self.contract)
        contract['discount_basis'] = 'shipment'
        self.data['positions'][2]['quantity'] = 60
        result = account(contract, self.scenario, self.data)
        self.assertIn('ship-1', result['discount']['orders_with_tier_change'])
        order = next(row for row in result['orders'] if row['id'] == 'ship-1')
        self.assertAlmostEqual(order['discount_original_rate'], 0.07)
        self.assertEqual(order['discount_final_rate'], 0)
        self.assertGreater(Decimal(order['discount_true_up']), 0)

    def test_discount_basis_is_declared(self):
        # Annual commitment is the default: one tier for the whole year's net
        # units, so months are not treated as independent orders.
        result = account(self.contract, self.scenario, self.data)
        self.assertEqual(result['discount_basis'], 'annual')
        self.assertEqual(result['discount']['final_rate'], 0.07)
        contract = copy.deepcopy(self.contract)
        contract['discount_basis'] = 'shipment'
        result = account(contract, self.scenario, self.data)
        self.assertEqual(result['discount_basis'], 'shipment')
        self.assertIsNone(result['discount']['final_rate'])

    def test_zero_delivery_has_no_margin_rate(self):
        empty = copy.deepcopy(self.data)
        empty['positions'] = []
        scenario = copy.deepcopy(self.scenario)
        scenario['months'] = []
        result = account(self.contract, scenario, empty, {'include_recorded': False})
        self.assertEqual(result['units']['net'], 0)
        self.assertIsNone(result['margin_rate'])
        self.assertEqual(Decimal(result['margin']), 0)

    def test_return_cannot_exceed_delivery(self):
        self.data['positions'][2]['quantity'] = 200
        with self.assertRaises(ValueError):
            account(self.contract, self.scenario, self.data)

    def test_float_input_does_not_distort_thresholds(self):
        # 0.07 must mean decimal 0.07, not the binary float 0.07000000000000001.
        scenario = copy.deepcopy(self.scenario)
        scenario['months'] = []
        scenario['quantity'] = 0
        empty = copy.deepcopy(self.data)
        empty['positions'] = []
        contract = copy.deepcopy(self.contract)
        result = account(contract, scenario, empty, {'include_recorded': False})
        self.assertEqual(Decimal(result['margin']), Decimal(0))

    def test_allocate_preserves_shape(self):
        self.assertEqual(allocate(100, [90, 110]), [45, 55])
        self.assertEqual(allocate(3, [1, 1]), [2, 1])
        self.assertEqual(allocate(0, [1, 2]), [0, 0])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()

    def test_analysis_requires_floor_and_pins_digest(self):
        with self.assertRaises(ValueError):
            analyse(self.data, {})
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1']})
        self.assertEqual(len(result['data_digest']), 64)
        self.assertEqual(result['exact'][0]['contract_digest'], result['contracts'][0]['digest'])

    def test_exact_matches_annual_account(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1']})
        row = result['exact'][0]
        direct = account(self.data['contracts'][0], self.data['scenarios'][0], self.data)
        self.assertEqual(row['account']['margin'], direct['margin'])
        self.assertEqual(row['account']['composition'], direct['composition'])

    def test_feasible_region_and_cliff_explanation(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s2'],
                                     'search': {'min_path_total': 0, 'max_path_total': 400}})
        region = result['feasible_regions'][0]
        self.assertTrue(region['feasible'])
        self.assertEqual(region['intervals'], [[0, 9], [77, 259]])
        crossing = next(row for row in region['transitions']
                        if row['direction'] == 'leave_feasible' and row['path_total'] == 10)
        self.assertEqual(crossing['triggers'][0]['clause'], 'annual_rebate')
        self.assertEqual(crossing['triggers'][0]['threshold_units'], 250)
        # Selling more can earn less: margin falls at the tier crossing.
        self.assertLess(crossing['margin_after'], crossing['margin_before'])
        self.assertGreater(crossing['margin_rate_after'] - 0.18, -1)

    def test_region_keeps_joint_conditions(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s2'],
                                     'search': {'min_path_total': 10, 'max_path_total': 30}})
        region = result['feasible_regions'][0]
        self.assertEqual(region['method']['joint_conditions'], '每个枚举点保持该情景声明的每月汇率、运费与退单条件联动')
        self.assertTrue(region['method']['shared_path'])

    def test_volume_currency_freight_overrides(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'overrides': {'s1': {'quantity': 300}}})
        self.assertEqual(result['exact'][0]['account']['units']['net'], 540)
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'overrides': {'s1': {'currency': 'EUR'}}})
        self.assertEqual(result['exact'][0]['applied_overrides'], {'currency': 'EUR'})
        self.assertEqual(set(result['exact'][0]['account']['currency_flows']), {'EUR'})
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'overrides': {'s1': {'freight_multiplier': 2}}})
        self.assertEqual(Decimal(result['exact'][0]['account']['freight']), Decimal('2400'))

    def test_freight_floor_and_unit_overrides(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'overrides': {'s1': {'freight_floor': 500, 'freight_unit': 2}}})
        account_result = result['exact'][0]['account']
        self.assertEqual(result['exact'][0]['applied_overrides'], {'freight_floor': 500, 'freight_unit': 2})
        for order in account_result['orders']:
            expected = max(Decimal('500'), Decimal(order['shipped_units']) * Decimal('2')) * Decimal('1')
            self.assertEqual(Decimal(order['freight']), expected)

    def test_monthly_override_keeps_each_month_conditions(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'overrides': {'s1': {'months': [
                                         {'month': 4, 'quantity': 60, 'fx': {'USD': 7.5, 'EUR': 7.9}},
                                         {'month': 5, 'quantity': 40, 'fx': {'USD': 6.5, 'EUR': 7.1}},
                                     ]}}})
        account_result = result['exact'][0]['account']
        forecast = {order['month']: order for order in account_result['orders'] if order['source'] == 'forecast'}
        self.assertEqual(Decimal(forecast[4]['fx_rate']), Decimal('7.5'))
        self.assertEqual(Decimal(forecast[5]['fx_rate']), Decimal('6.5'))
        self.assertEqual(forecast[4]['shipped_units'], 60)
        self.assertEqual(forecast[5]['shipped_units'], 40)

    def test_same_total_different_monthly_shape_changes_margin(self):
        # The whole-year path matters: with month 4 and month 5 FX differing,
        # front-loading into the stronger-FX month earns more at the same total.
        base = {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1']}
        front_loaded = analyse(self.data, dict(base, overrides={'s1': {'months': [
            {'month': 4, 'quantity': 150, 'fx': {'USD': 7.6, 'EUR': 7.9}},
            {'month': 5, 'quantity': 50, 'fx': {'USD': 6.6, 'EUR': 7.2}},
        ]}}))
        back_loaded = analyse(self.data, dict(base, overrides={'s1': {'months': [
            {'month': 4, 'quantity': 50, 'fx': {'USD': 7.6, 'EUR': 7.9}},
            {'month': 5, 'quantity': 150, 'fx': {'USD': 6.6, 'EUR': 7.2}},
        ]}}))
        self.assertGreater(Decimal(front_loaded['exact'][0]['account']['margin']),
                           Decimal(back_loaded['exact'][0]['account']['margin']))

    def test_quantity_and_months_override_conflict(self):
        with self.assertRaises(ValueError):
            analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                'overrides': {'s1': {'quantity': 100, 'months': [{'month': 4, 'quantity': 100}]}}})

    def test_annual_basis_cliff_names_one_discount_clause(self):
        data = copy.deepcopy(self.data)
        contract = data['contracts'][0]
        contract['discount_basis'] = 'annual'
        contract['tiers'] = [{'minimum': 0, 'discount': 0}, {'minimum': 250, 'discount': 0.15}]
        result = analyse(data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                'search': {'min_path_total': 0, 'max_path_total': 60}})
        region = result['feasible_regions'][0]
        crossing = next(cliff for cliff in region['cliffs'] if cliff['path_total'] == 10)
        clauses = [trigger['clause'] for trigger in crossing['triggers']]
        self.assertIn('annual_rebate', clauses)
        discount = [trigger for trigger in crossing['triggers'] if trigger['clause'] == 'quantity_discount']
        self.assertEqual(len(discount), 1)
        self.assertEqual(discount[0]['basis'], 'annual')
        self.assertEqual(discount[0]['rate_after'], 0.15)

    def test_infeasible_region_is_explicit(self):
        result = analyse(self.data, {'margin_floor': 0.40, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'search': {'min_path_total': 0, 'max_path_total': 50}})
        region = result['feasible_regions'][0]
        self.assertFalse(region['feasible'])
        self.assertEqual(region['intervals'], [])
        self.assertIsNone(region['min_path_total'])

    def test_base_currency_settlement_uses_identity_fx(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                     'overrides': {'s1': {'currency': 'CNY'}}})
        applied = result['exact'][0]['applied_overrides']
        self.assertEqual(applied['currency'], 'CNY')
        self.assertEqual(applied['implied_fx'], {'CNY': 1.0})
        flows = result['exact'][0]['account']['currency_flows']
        self.assertEqual(Decimal(flows['CNY']['weighted_fx']), Decimal('1'))

    def test_unknown_override_rejected(self):
        with self.assertRaises(ValueError):
            analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                'overrides': {'s1': {'unexpected': 1}}})

    def test_currency_override_needs_declared_fx(self):
        with self.assertRaises(ValueError):
            analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1'],
                                'overrides': {'s1': {'currency': 'JPY'}}})

    def test_comparison_uses_same_scenario_set(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a', 'offer-b'],
                                     'scenarios': ['s1', 's2']})
        cells = result['comparison']['cells']
        self.assertEqual(len(cells), 4)
        for cell in cells:
            self.assertEqual(cell['scenario'], 's1' if cell['scenario'] == 's1' else 's2')
        # Every cell names the scenario it was computed in; no mixed conditions.
        self.assertEqual({cell['scenario'] for cell in cells}, {'s1', 's2'})
        self.assertEqual(result['comparison']['scenario_wins'], {'offer-a': 2, 'offer-b': 0})

    def test_risk_is_finite_scope_only(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'], 'scenarios': ['s1', 's2']})
        risk = result['risk']
        self.assertEqual(risk['scope'], 'finite_declared_scenarios')
        self.assertIn('跨情景拼接的最坏情况', risk['not_supported'])
        self.assertEqual(len(risk['observations']), 2)

    def test_region_intersection_requires_common_total(self):
        result = analyse(self.data, {'margin_floor': 0.18, 'contracts': ['offer-a'],
                                     'scenarios': ['s1', 's2', 's3'],
                                     'search': {'min_path_total': 0, 'max_path_total': 400}})
        intersection = result['region_intersections'][0]
        self.assertTrue(intersection['feasible'])
        self.assertEqual(intersection['intervals'], [[0, 9], [77, 259]])


class AnalysisHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.repository = Repository(Path(self.directory.name) / 'data.sqlite3')
        self.data = load_fixture()
        self.receipt = self.repository.import_data(self.data, 'seed')
        self.server = make_server(self.repository)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def test_analysis_requires_revision(self):
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/analysis', {'options': {'margin_floor': 0.18}})
        self.assertEqual(caught.exception.code, 400)

    def test_analysis_roundtrip_and_revision_pinning(self):
        response = self.client.analyze(self.receipt['revision'], {'margin_floor': 0.18, 'contracts': ['offer-a'],
                                                                  'scenarios': ['s1']})
        self.assertEqual(response['revision'], self.receipt['revision'])
        self.assertEqual(response['digest'], self.receipt['digest'])
        self.assertEqual(response['result']['data_digest'], response['digest'])
        first_margin = response['result']['exact'][0]['account']['margin']
        changed = copy.deepcopy(self.data)
        changed['contracts'][0]['price'] = 20
        second = self.client.import_data(changed, 'changed')
        old = self.client.analyze(self.receipt['revision'], {'margin_floor': 0.18, 'contracts': ['offer-a'],
                                                             'scenarios': ['s1']})
        self.assertEqual(old['result']['exact'][0]['account']['margin'], first_margin)
        self.assertNotEqual(old['result']['data_digest'], second['digest'])

    def test_bad_revision_and_options(self):
        with self.assertRaises(HTTPError) as caught:
            self.client.analyze(999, {'margin_floor': 0.18})
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.client.analyze(self.receipt['revision'], {})
        self.assertEqual(caught.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
