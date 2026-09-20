"""Executable architecture examples; do not certify the application or tax law."""
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DDL = ROOT / 'docs' / 'contracts' / 'ledger.sql'

class LedgerContractTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript(DDL.read_text(encoding='utf-8'))
        self.db.execute("INSERT INTO book VALUES(1,'example-entity','IRR')")
        self.db.execute("INSERT INTO fiscal_years VALUES('y1','2026-01-01','2026-12-31')")
        self.db.execute("INSERT INTO fiscal_years VALUES('y2','2027-01-01','2027-12-31')")
        self.db.execute("INSERT INTO periods VALUES('p1','y1','2026-01-01','2026-12-31','open')")
        self.db.execute("INSERT INTO periods VALUES('p2','y2','2027-01-01','2027-12-31','open')")
        self.db.execute("INSERT INTO accounts VALUES('1','Assets','group',NULL,0,1)")
        self.db.execute("INSERT INTO accounts VALUES('10','Cash','kol','1',0,1)")
        self.db.execute("INSERT INTO accounts VALUES('1010','Bank','moein','10',0,1)")
        self.db.execute("INSERT INTO accounts VALUES('1020','Receivable','moein','10',1,1)")
        self.db.execute("INSERT INTO tafsili_accounts VALUES('party1','Example','person',1)")
        self.db.execute("INSERT INTO tafsili_accounts VALUES('proj1','Project','project',1)")

    def tearDown(self):
        self.db.close()

    def draft(self, key='v1', year='y1', period='p1', date='2026-09-20'):
        self.db.execute("INSERT INTO journal_vouchers(id,fiscal_year_id,period_id,accounting_date,description,idempotency_key,request_sha256) VALUES(?,?,?,?,?,?,?)",
                        (key,year,period,date,'example',key,'a'*64))

    def line(self, key='v1', row=1, debit=100, credit=0, account='1010', tafsili=None):
        self.db.execute("INSERT INTO voucher_items VALUES(?,?,?,?,?,?,?,?)",
                        (key+'-'+str(row),key,row,account,tafsili,'example',debit,credit))

    def balanced(self, key='v1', **kwargs):
        self.draft(key, **kwargs)
        self.line(key)
        self.line(key,2,0,100)

    def post(self, key='v1', number=1):
        self.db.execute("UPDATE journal_vouchers SET status='posted',voucher_number=?,approved_by='human-1',posted_at='2026-09-20T00:00:00Z' WHERE id=?", (number,key))

    def test_balanced_voucher_posts_and_totals_are_derived(self):
        self.balanced(); self.post()
        self.assertEqual(self.db.execute('SELECT total_debit_irr,total_credit_irr,item_count FROM voucher_totals').fetchone(),(100,100,2))

    def test_unbalanced_voucher_rejected(self):
        self.draft(); self.line(); self.line(row=2,debit=0,credit=1)
        with self.assertRaisesRegex(sqlite3.IntegrityError,'UNBALANCED'):
            self.post()

    def test_empty_voucher_rejected(self):
        self.draft()
        with self.assertRaisesRegex(sqlite3.IntegrityError,'LINE_COUNT'):
            self.post()

    def test_zero_or_double_sided_lines_rejected(self):
        self.draft()
        for debit,credit in [(0,0),(1,1),(-1,0),(0,-1)]:
            with self.subTest(debit=debit,credit=credit), self.assertRaises(sqlite3.IntegrityError):
                self.line(debit=debit,credit=credit)

    def test_fractional_money_rejected(self):
        self.draft()
        with self.assertRaises(sqlite3.IntegrityError):
            self.line(debit=1.5)

    def test_duplicate_row_rejected(self):
        self.draft(); self.line()
        with self.assertRaises(sqlite3.IntegrityError):
            self.line()

    def test_closed_period_rejected(self):
        self.balanced(); self.db.execute("UPDATE periods SET state='closed'")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'PERIOD'):
            self.post()

    def test_out_of_period_date_rejected(self):
        self.balanced(date='2027-01-01')
        with self.assertRaisesRegex(sqlite3.IntegrityError,'PERIOD'):
            self.post()

    def test_invalid_calendar_date_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.draft(date='not-a-date')

    def test_fiscal_year_number_scope(self):
        self.balanced(); self.post()
        self.balanced('v2',year='y2',period='p2',date='2027-01-01'); self.post('v2',1)
        self.balanced('v3')
        with self.assertRaises(sqlite3.IntegrityError):
            self.post('v3',1)

    def test_direct_posted_insert_rejected(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError,'CREATE_DRAFT_FIRST'):
            self.db.execute("INSERT INTO journal_vouchers(id,fiscal_year_id,period_id,accounting_date,description,status,idempotency_key,request_sha256) VALUES('x','y1','p1','2026-09-20','x','posted','x',?)",('b'*64,))

    def test_posted_header_update_delete_rejected(self):
        self.balanced(); self.post()
        for query in ["UPDATE journal_vouchers SET description='changed'",'DELETE FROM journal_vouchers']:
            with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError,'POSTED_IMMUTABLE'):
                self.db.execute(query)

    def test_posted_line_update_delete_insert_rejected(self):
        self.balanced(); self.post()
        for query in ['UPDATE voucher_items SET debit_irr=200 WHERE row_order=1','DELETE FROM voucher_items']:
            with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError,'POSTED_IMMUTABLE'):
                self.db.execute(query)
        with self.assertRaisesRegex(sqlite3.IntegrityError,'POSTED_IMMUTABLE'):
            self.line(row=3)

    def test_inactive_and_non_leaf_account_rejected(self):
        self.balanced(); self.db.execute("UPDATE accounts SET is_active=0 WHERE code='1010'")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'ACCOUNT'):
            self.post()
        self.db.execute("UPDATE accounts SET is_active=1")
        self.db.execute("UPDATE voucher_items SET account_code='10'")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'ACCOUNT'):
            self.post()

    def test_required_tafsili(self):
        self.balanced(); self.db.execute("UPDATE voucher_items SET account_code='1020'")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'TAFSILI'):
            self.post()
        self.db.execute("UPDATE voucher_items SET tafsili_code='party1'"); self.post()

    def test_simultaneous_dimensions_and_immutability(self):
        self.balanced()
        self.db.execute("INSERT INTO item_dimensions VALUES('v1-1','party','party1')")
        self.db.execute("INSERT INTO item_dimensions VALUES('v1-1','project','proj1')")
        self.post()
        for query in ["DELETE FROM item_dimensions", "UPDATE item_dimensions SET tafsili_code='party1'", "INSERT INTO item_dimensions VALUES('v1-2','party','party1')"]:
            with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError,'POSTED_IMMUTABLE'):
                self.db.execute(query)

    def test_wrong_dimension_kind_rejected(self):
        self.balanced()
        self.db.execute("INSERT INTO item_dimensions VALUES('v1-1','project','party1')")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'DIMENSION'):
            self.post()

    def test_reversal_requires_posted_original(self):
        self.balanced('original'); self.balanced()
        self.db.execute("UPDATE journal_vouchers SET reversal_of='original' WHERE id='v1'")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'REVERSAL'):
            self.post()

    def test_idempotency_key_unique(self):
        self.draft()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO journal_vouchers(id,fiscal_year_id,period_id,accounting_date,description,idempotency_key,request_sha256) VALUES('other','y1','p1','2026-09-20','x','v1',?)",('a'*64,))

    def test_audit_append_only_and_chain(self):
        self.db.execute('INSERT INTO audit_events VALUES(1,?,?,?,?,?,?,?,?)',('e1','actor','r1','POSTED','2026-09-20T00:00:00Z','{}','0'*64,'a'*64))
        for query in ['DELETE FROM audit_events',"UPDATE audit_events SET actor_id='other'"]:
            with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError,'AUDIT_APPEND_ONLY'):
                self.db.execute(query)
        with self.assertRaisesRegex(sqlite3.IntegrityError,'AUDIT_CHAIN'):
            self.db.execute('INSERT INTO audit_events VALUES(2,?,?,?,?,?,?,?,?)',('e2','actor','r2','POSTED','2026-09-20T00:00:00Z','{}','b'*64,'c'*64))
        self.db.execute('INSERT INTO audit_events VALUES(2,?,?,?,?,?,?,?,?)',('e2','actor','r2','POSTED','2026-09-20T00:00:00Z','{}','a'*64,'c'*64))

    def test_single_entity_per_database(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO book VALUES(2,'other','IRR')")

    def test_book_identity_is_immutable(self):
        for query in ["UPDATE book SET entity_id='other'", "DELETE FROM book", "INSERT OR REPLACE INTO book VALUES(1,'other','IRR')"]:
            with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError, 'BOOK_IDENTITY_IMMUTABLE'):
                self.db.execute(query)

    def test_replace_cannot_remove_posted_line(self):
        self.balanced(); self.post(); self.draft('v2')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'POSTED_IMMUTABLE'):
            self.db.execute("INSERT OR REPLACE INTO voucher_items VALUES('v1-1','v2',1,'1010',NULL,'x',100,0)")

    def test_replace_cannot_remove_audit_event(self):
        self.db.execute('INSERT INTO audit_events VALUES(1,?,?,?,?,?,?,?,?)', ('e1','actor','r1','POSTED','2026-09-20T00:00:00Z','{}','0'*64,'a'*64))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'AUDIT_APPEND_ONLY'):
            self.db.execute('INSERT OR REPLACE INTO audit_events VALUES(2,?,?,?,?,?,?,?,?)', ('e1','actor','r2','POSTED','2026-09-20T00:00:00Z','{}','a'*64,'b'*64))

    def test_recursive_triggers_enabled(self):
        self.assertEqual(self.db.execute('PRAGMA recursive_triggers').fetchone()[0], 1)

    def test_foreign_keys_enabled(self):
        self.assertEqual(self.db.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.draft()
        with self.assertRaises(sqlite3.IntegrityError):
            self.line(account='missing')

if __name__ == '__main__':
    unittest.main()
