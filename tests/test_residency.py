"""Tests for the roster reader (sdmmodern/residency.py). Run: python -m unittest tests.test_residency"""
import csv, io, os, unittest, zipfile
from sdmmodern import residency as R

HERE = os.path.dirname(os.path.abspath(__file__))


def _csv(rows):
    b = io.StringIO()
    csv.writer(b).writerows(rows)
    return b.getvalue().encode()


LONG = [['Member', 'Master stable', 'Stable', 'Joined', 'Left'],
        ['Ann', 'Alpha', 'Alpha', '', ''], ['Ann', 'Alpha', 'Beta', '3/2/2026', ''],
        ['Bo', 'Gamma', 'Gamma', '', '']]


class Long(unittest.TestCase):
    def test_basic(self):
        r = R.build('r.csv', _csv(LONG), code='orphans')
        self.assertTrue(r['ok']); self.assertEqual(r['counts']['stables'], 3)
        self.assertEqual(r['stables'][1]['joined'], '2026-03-02')

    def test_bad_date_and_order(self):
        rows = LONG + [['Bo', 'Gamma', 'Delta', 'soon', ''], ['Bo', 'Gamma', 'Eps', '2026-05-01', '2026-01-01']]
        r = R.build('r.csv', _csv(rows), code='orphans')
        self.assertEqual(r['counts']['errors'], 2); self.assertEqual(r['counts']['stables'], 3)

    def test_stable_under_two_members(self):
        r = R.build('r.csv', _csv(LONG + [['Bo', 'Gamma', 'Beta', '', '']]), code='orphans')
        self.assertFalse(r['ok'])
        self.assertNotIn('beta', [s['key'] for s in r['stables']])

    def test_exact_repeat_is_a_warning(self):
        r = R.build('r.csv', _csv(LONG + [['Ann', 'Alpha', 'Beta', '3/2/2026', '']]), code='orphans')
        self.assertTrue(r['ok']); self.assertEqual(r['counts']['stables'], 3)

    def test_headings_and_example_rows(self):
        rows = [['Player', 'Master', 'Stable Name'], ['Example Member', 'x', 'y'], ['Ann', 'A', 'A']]
        r = R.build('r.csv', _csv(rows), code='orphans')
        self.assertEqual(r['counts']['stables'], 1)

    def test_bad_code_and_file(self):
        with self.assertRaises(R.RosterError): R.build('r.csv', _csv(LONG), code='Bad Code!')
        with self.assertRaises(R.RosterError): R.build('r.csv', b'nothing useful\n', code='orphans')
        with self.assertRaises(R.RosterError): R.build('r.xlsx', b'PKnot a zip', code='orphans')


class Grid(unittest.TestCase):
    def test_csv_grid(self):
        rows = [['Member', 'Ann', '', 'Bo'], ['Master stable', 'M1', 'M2', 'M3'],
                ['Stables', 's1', 's3', 's5'], ['', 's2', '', 's6']]
        r = R.build('g.csv', _csv(rows), code='orphans')
        self.assertEqual([(m['name'], [x['name'] for x in m['masters']]) for m in r['members']],
                         [('Ann', ['M1', 'M2']), ('Bo', ['M3'])])
        self.assertEqual(r['counts']['stables'], 5)

    def test_big_member_23_masters_100_stables(self):
        rows = [['Member', 'Ann'] + [''] * 22, ['Master'] + ['M%d' % i for i in range(23)]]
        for k in range(100):
            rows.append([''] + ['s%d-%d' % (i, k) for i in range(23)])
        r = R.build('g.csv', _csv(rows), code='orphans')
        self.assertEqual(r['counts']['masters'], 23); self.assertEqual(r['counts']['stables'], 2300)
        self.assertEqual(r['counts']['members'], 1)

    def test_conflict_names_the_grid_cell(self):
        rows = [['Member', 'Ann', 'Bo'], ['Master', 'M1', 'M2'], ['', 'dup', 'dup']]
        r = R.build('g.csv', _csv(rows), code='orphans')
        self.assertFalse(r['ok']); self.assertIn('grid column', r['problems']['errors'][0]['message'])


class Templates(unittest.TestCase):
    def test_shipped_templates_read_cleanly_and_are_empty(self):
        d = os.path.join(os.path.dirname(HERE), 'residency')
        for fn in ('roster-template.xlsx', 'roster-template.csv'):
            p = os.path.join(d, fn)
            if not os.path.exists(p):
                self.skipTest('template not in this checkout')
            r = R.build(fn, open(p, 'rb').read(), code='orphans')
            self.assertTrue(r['ok']); self.assertEqual(r['counts']['stables'], 0)


class MemberTabs(unittest.TestCase):
    def test_one_tab_per_member(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed')
        wb = openpyxl.Workbook(); a = wb.active; a.title = 'Ann'
        a['A1'], a['B1'], a['B2'], a['C2'], a['B3'], a['C3'] = 'Member', 'Ann', 'M1', 'M2', 'a1', 'a2'
        b = wb.create_sheet('Bo'); b['A1'], b['B1'], b['B2'], b['B3'] = 'Member', 'Bo', 'M3', 'b1'
        buf = io.BytesIO(); wb.save(buf)
        r = R.build('m.xlsx', buf.getvalue(), code='orphans')
        self.assertTrue(r['ok']); self.assertEqual(r['counts'], dict(members=2, masters=3, stables=3, errors=0, warnings=1))
        b['B4'] = 'a1'; buf = io.BytesIO(); wb.save(buf)
        r = R.build('m.xlsx', buf.getvalue(), code='orphans')
        self.assertFalse(r['ok']); self.assertIn('tab ', r['problems']['errors'][0]['message'])


class DbMatch(unittest.TestCase):
    def test_match_and_close_names(self):
        import sqlite3, tempfile
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        c = sqlite3.connect(db)
        c.executescript("CREATE TABLE owners(id INTEGER PRIMARY KEY, oname TEXT, o15 TEXT, o12 TEXT, o10 TEXT);"
                        "CREATE TABLE entries(id INTEGER PRIMARY KEY, week INTEGER, owner_id INTEGER, fin INTEGER);"
                        "INSERT INTO owners(id,oname) VALUES(1,'Alpha'),(2,'alpha'),(3,'Gamma Farms');"
                        "INSERT INTO entries(week,owner_id,fin) VALUES(5,1,1),(6,2,3),(7,3,1);")
        c.commit(); c.close()
        r = R.build('r.csv', _csv([['Member', 'Master stable', 'Stable'], ['Ann', 'Alpha', 'Alpha'], ['Ann', 'Alpha', 'Gamma Farm'], ['Bo', 'Zed', 'Zed']]), code='orphans')
        rep = R.match_db(r, db)
        self.assertEqual((rep['checked'], rep['matched'], rep['unmatched']), (3, 1, 2))
        a = rep['stables'][0]; self.assertEqual((a['races'], a['wins'], a['first_week'], a['last_week']), (2, 1, 5, 6))
        self.assertIn('gamma farms', rep['stables'][1]['close'])
        self.assertFalse(os.path.exists(db + '-wal'))


class Safety(unittest.TestCase):
    def test_xml_entities_rejected(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, 'w') as z:
            z.writestr('xl/workbook.xml', '<!DOCTYPE x [<!ENTITY a "b">]><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
        with self.assertRaises(R.RosterError): R.build('r.xlsx', b.getvalue(), code='orphans')

    def test_too_big(self):
        with self.assertRaises(R.RosterError): R.build('r.csv', b'x' * (R.MAX_FILE + 1), code='orphans')


if __name__ == '__main__':
    unittest.main()
