import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app import DATA, Conflict, Review, StyledText, blocks, digest, handler, segments


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'meid-am.txt'
        self.source.write_bytes((DATA / 'applied-original.txt').read_bytes())
        self.review = Review(self.source, self.root / 'state')

    def edit(self, date):
        entry = self.review.entry(date)
        fields = [f['text'] for f in entry['fields']]
        fields[1] += ' Kalimat uji dengan <i>cetak miring</i>.'
        return entry, fields

    def test_save_preserves_structure_and_other_dates_with_backup(self):
        before = self.source.read_bytes()
        index = self.review.index(before.decode())
        entry, fields = self.edit('0102')
        self.review.mark('R001', True, entry['version'])
        result = self.review.save('0102', entry['version'], fields)
        after = self.review.index(self.source.read_text())
        self.assertEqual({d: b for d, b in index.items() if d != '0102'}, {d: b for d, b in after.items() if d != '0102'})
        self.assertEqual(index['0102'].split('*a')[0], after['0102'].split('*a')[0])
        self.assertIn('<i>cetak miring</i>', after['0102'])
        self.assertFalse(result['checks']['R001'])
        self.assertEqual(next((self.root / 'state/backups').iterdir()).read_bytes(), before)
        self.assertEqual([d for d, _, _ in blocks(before.decode())], [d for d, _, _ in blocks(self.source.read_text())])

    def test_conflict_does_not_overwrite_newer_edit(self):
        entry, fields = self.edit('0102')
        self.review.save('0102', entry['version'], fields)
        after = self.source.read_bytes()
        with self.assertRaises(Conflict): self.review.save('0102', entry['version'], fields)
        self.assertEqual(self.source.read_bytes(), after)
        with self.assertRaises(Conflict): self.review.mark('R001', True, entry['version'])

    def test_external_edit_preserved(self):
        entry, fields = self.edit('0102')
        self.source.write_text(self.source.read_text().replace('Menarik untuk diperhatikan', 'Perhatikanlah', 1))
        changed = self.source.read_bytes()
        with self.assertRaises(Conflict): self.review.save('0102', entry['version'], fields)
        self.assertEqual(self.source.read_bytes(), changed)

    def test_duplicate_date_updated_identically(self):
        entry, fields = self.edit('1027')
        self.review.save('1027', entry['version'], fields)
        text = self.source.read_text()
        duplicates = [text[a:z] for d, a, z in blocks(text) if d == '1027']
        self.assertEqual(len(duplicates), 2)
        self.assertEqual(duplicates[0], duplicates[1])
        self.assertIn('Kalimat uji', duplicates[0])

    def test_progress_survives_restart_and_import_merges(self):
        entry = self.review.entry('0102')
        self.review.mark('R001', True, entry['version'])
        other = Review(self.source, self.root / 'state')
        self.assertTrue(other.progress(other.index(other.read()))['R001'])
        result = other.import_progress({'revision': '20261012', 'checks': {'R001': False, 'R002': True}})
        self.assertTrue(result['checks']['R001'])
        self.assertTrue(result['checks']['R002'])
        other.mark('R001', False, entry['version'])
        self.assertFalse(other.progress(other.index(other.read()))['R001'])

    def test_invalid_markup_and_structure_never_write(self):
        before = self.source.read_bytes()
        for text in ['<script>bad()</script>', '<i>unclosed', 'text\nmmdd 0103', 'text\n*n', '</i>bad<i>']:
            entry, fields = self.edit('0102')
            fields[1] = text
            with self.assertRaises(ValueError): self.review.save('0102', entry['version'], fields)
            self.assertEqual(self.source.read_bytes(), before)

    def test_all_highlights_and_italics_render(self):
        current = self.review.index(self.review.read())
        for record in self.review.records:
            for kind in ['english', 'current_indonesian', 'suggested_indonesian']:
                with self.subTest(id=record['id'], kind=kind):
                    output = self.review.column(record, kind, current)
                    self.assertIn('<strong>', output)
                    self.assertNotIn('<script', output)
            en = self.review.column(record, 'english', current)
            for phrase in self.review.highlights[record['id']]:
                self.assertIn(phrase, StyledText(en).text)
        first = self.review.column(self.review.records[0], 'english', current)
        self.assertIn('<strong>We scarcely open the Bible before we read</strong>', first)
        self.assertNotIn('<strong>It is interesting', first)
        sample = StyledText('before <i>important words</i> after')
        self.assertEqual(sample.render(0, len(sample.text), [(7, 16)]), 'before <strong><i>important</i></strong><i> words</i> after')
        page = self.review.page()
        self.assertEqual(page.count('class="finding"'), 0)
        self.assertIn('<i>', page)

    def test_custom_edits_still_have_highlights(self):
        entry = self.review.entry('0114')
        fields = [f['text'].replace('Kuasa Kristus tidak berhenti pada', 'Kuasa Kristus tidak berhenti hanya sampai') for f in entry['fields']]
        result = self.review.save('0114', entry['version'], fields)
        self.assertIn('<strong>Kuasa Kristus tidak berhenti hanya sampai', result['columns']['R008'])

    def test_every_entry_roundtrips_without_changes(self):
        before = self.source.read_bytes()
        for date in self.review.index(before.decode()):
            entry = self.review.entry(date)
            self.review.save(date, entry['version'], [f['text'] for f in entry['fields']])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse((self.root / 'state/backups').exists())

    def test_editor_highlights_follow_finding_and_never_enter_source(self):
        before = self.source.read_bytes()
        for ident in self.review.by_id:
            entry = self.review.edit(ident)
            self.assertTrue(any(field['highlights'] for field in entry['fields']), ident)
            for field in entry['fields']:
                for start, end in field['highlights']:
                    self.assertGreater(end, start)
                    self.assertLessEqual(end, len(field['text'].encode('utf-16-le')) // 2)
        first = self.review.edit('R013')
        second = self.review.edit('R014')
        self.assertEqual(first['date'], second['date'])
        self.assertNotEqual([f['highlights'] for f in first['fields']], [f['highlights'] for f in second['fields']])
        self.review.save(first['date'], first['version'], [f['text'] for f in first['fields']])
        self.assertEqual(self.source.read_bytes(), before)

    def test_editor_loads_external_changes(self):
        self.source.write_text(self.source.read_text().replace('Kuasa Kristus tidak berhenti pada', 'Kuasa Kristus tidak berhenti hanya sampai'))
        entry = self.review.edit('R008')
        self.assertTrue(any('tidak berhenti hanya sampai' in f['text'] for f in entry['fields']))
        self.assertTrue(any(f['highlights'] for f in entry['fields']))

    def test_corrected_findings_disappear_and_reverted_passage_returns(self):
        before = self.source.read_bytes()
        self.assertNotIn('class="finding"', self.review.page())
        self.assertEqual(before, self.source.read_bytes())
        record = self.review.by_id['R001']
        new = record['suggested_indonesian'][0]['snippet']
        old = record['current_indonesian'][0]['snippet']
        self.source.write_text(self.source.read_text().replace(new, old, 1))
        page = self.review.page()
        self.assertEqual(page.count('class="finding"'), 1)
        self.assertIn('id="R001"', page)
        entry = self.review.entry(record['date'])
        result = self.review.save(record['date'], entry['version'], [f['text'].replace(old, new) for f in entry['fields']])
        self.assertIn('R001', result['resolved'])
        self.assertNotIn('class="finding"', self.review.page())

    def test_partial_correction_remains(self):
        record = self.review.by_id['R004']
        snippet = 'Ini <i>keinginan dipuji</i>.'
        self.source.write_text(self.source.read_text().replace(snippet, 'Ini <i>kesia-siaan</i>.', 1))
        self.assertIn('id="R004"', self.review.page())

    def test_assessed_alternatives_and_formatting_are_recognized(self):
        text = self.source.read_text()
        for variants in self.review.variants.values():
            for original, alternatives in variants.items():
                raw = original.replace('dirimu sendiri', '<i>dirimu sendiri</i>') if 'dirimu sendiri' in original else original
                self.assertTrue(raw in text, original)
                text = text.replace(raw, '<i>' + alternatives[0].replace(' ', '  ') + '</i>', 1)
        self.source.write_text(text)
        self.assertNotIn('class="finding"', self.review.page())

    def test_proposal_is_read_only_and_applies_only_one_finding(self):
        self.source.write_bytes((DATA / 'meid-am.before-20261012.txt').read_bytes())
        before = self.source.read_bytes()
        entry = self.review.edit('R006')
        self.assertEqual(self.source.read_bytes(), before)
        proposal = '\n'.join(f['text'] for f in entry['fields'])
        self.assertIn('Ia telah membayar lunas harga penebusanmu', proposal)
        self.assertIn('Isakhar [Kejadian 30:18]', proposal)
        result = self.review.save(entry['date'], entry['version'], [f['text'] for f in entry['fields']], 'R006')
        self.assertIn('R006', result['resolved'])
        self.assertNotIn('R007', result['resolved'])
        original = self.review.index(before.decode())
        actual = self.review.index(self.source.read_text())
        self.assertEqual({k:v for k,v in original.items() if k != entry['date']}, {k:v for k,v in actual.items() if k != entry['date']})

    def test_custom_applied_wording_persists_across_other_finding(self):
        self.source.write_bytes((DATA / 'meid-am.before-20261012.txt').read_bytes())
        entry = self.review.edit('R006')
        fields = [f['text'].replace('Ia telah membayar lunas harga penebusanmu', 'Ia sudah melunasi harga penebusanmu') for f in entry['fields']]
        self.review.save(entry['date'], entry['version'], fields, 'R006')
        entry = self.review.edit('R007')
        self.assertTrue(any('Ia sudah melunasi' in f['text'] for f in entry['fields']))
        self.review.save(entry['date'], entry['version'], [f['text'] for f in entry['fields']], 'R007')
        other = Review(self.source, self.root / 'state')
        current = other.index(other.read())
        self.assertTrue(other.resolved(other.by_id['R006'], current))
        self.source.write_text(self.source.read_text().replace('Ia sudah melunasi', 'Ia telah menghitung'))
        self.assertFalse(other.resolved(other.by_id['R006'], other.index(other.read())))

    def test_changed_passage_is_preserved_for_manual_merge(self):
        self.source.write_bytes((DATA / 'meid-am.before-20261012.txt').read_bytes())
        self.source.write_text(self.source.read_text().replace('Ia telah menghitung harga untuk penebusanmu', 'Kalimat yang sudah saya periksa'))
        entry = self.review.edit('R006')
        self.assertEqual(len(entry['warnings']), 1)
        self.assertTrue(any('Kalimat yang sudah saya periksa' in f['text'] for f in entry['fields']))

    def test_whole_reading_proposal_preserves_metadata_and_new_paragraph_kinds(self):
        self.source.write_bytes((DATA / 'meid-am.before-20261012.txt').read_bytes())
        before = self.review.index(self.source.read_text())['1031']
        entry = self.review.edit('R195')
        self.assertEqual([f['kind'] for f in entry['fields']], ['a', 'n', 'n', 'n'])
        self.review.save('1031', entry['version'], [f['text'] for f in entry['fields']], 'R195')
        after = self.review.index(self.source.read_text())['1031']
        self.assertEqual(before.split('*a')[0], after.split('*a')[0])
        self.assertEqual([f['kind'] for f in segments(after)], ['a', 'n', 'n', 'n'])
        self.assertIn('Mazmur 51:12', after)

    def test_http_rejects_cross_origin_and_missing_token(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.review))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        entry = self.review.entry('0102')
        data = json.dumps({'id': 'R001', 'checked': True, 'version': entry['version']}).encode()
        for origin, token in [('https://example.invalid', self.review.token), (base, 'wrong')]:
            request = urllib.request.Request(base + '/api/mark', data=data, headers={'Origin': origin, 'X-Review-Token': token})
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
        request = urllib.request.Request(base + '/api/mark', data=data, headers={'Origin': base, 'X-Review-Token': self.review.token})
        self.assertTrue(json.load(urllib.request.urlopen(request))['checks']['R001'])


if __name__ == '__main__':
    unittest.main()
