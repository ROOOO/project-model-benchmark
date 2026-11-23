"""Anonymous topic views retain exact evidence and reject mutation after sealing."""
from pathlib import Path
import copy
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
from benchmark import parser
from fresh_blind import anonymous_view, check_topic_seal, prepare_topic, restore_quote
from evaluation import formal_v3_gold, v4_gold_from_v3, v4_score_from_v3
from review import validate_review_scores
from test_scoring_v3 import sample_score, v3_gold


class FreshBlindTests(unittest.TestCase):
    def test_frozen_question_link_hides_backend_prefix_but_keeps_file_and_line(self):
        raw='[S10](<C:/Users/a/AppData/Local/agy-benchmark-work/opaque/readonly/newjoy/prompt.txt:586>)'
        view, edits=anonymous_view(raw)
        self.assertEqual(view,'[S10](<materials/newjoy/prompt.txt:586>)')
        self.assertEqual(restore_quote(view,view,raw,edits),raw)
        plain='[question](C:\\Temp\\agy-benchmark-work\\opaque\\readonly\\question.txt#L6)'
        v,e=anonymous_view(plain)
        self.assertEqual(v,'[question](materials/question.txt#L6)')
        self.assertEqual(restore_quote(v,v,plain,e),plain)

    def test_identity_prefixes_hidden_and_quotes_return_to_original_bytes(self):
        raw='结论 [x](file:///C:/Users/a/agy-benchmark-work/id/readonly/source/engineering/UE/x.cpp#L12) 和 /C:/Temp/codex-benchmark-id/scratch/local_test.py'
        view, edits=anonymous_view(raw)
        self.assertNotIn('agy-',view);self.assertNotIn('codex-',view)
        self.assertIn('frozen/engineering/UE/x.cpp#L12',view)
        self.assertIn('verification/local_test.py',view)
        self.assertEqual(restore_quote(view,view,raw,edits),raw)
        quote='[x](frozen/engineering/UE/x.cpp#L12)'
        restored=restore_quote(quote,view,raw,edits)
        self.assertIn(restored,raw);self.assertIn('x.cpp#L12',restored)
        args=parser().parse_args(['validate-topic','--packet','packet'])
        self.assertEqual(args.packet,'packet')

    def test_snapshot_source_links_hide_trial_identity_and_restore_exact_quotes(self):
        raw=(r'[UE](C:/Users/a/benchmarks/newjoy-ab/input/source-snapshot/engineering/UE/x.cpp:12)'
             r' and C:\Users\a\benchmarks\newjoy-ab\input\source-snapshot\engineering\UE\x.cpp:12'
             r' and C:/Assets/source-snapshot/textures/ui.png')
        view,edits=anonymous_view(raw)
        self.assertNotIn('newjoy-ab',view)
        self.assertIn('frozen/engineering/UE/x.cpp:12',view)
        self.assertIn('frozen/engineering\\UE\\x.cpp:12',view)
        self.assertIn('C:/Assets/source-snapshot/textures/ui.png',view)
        self.assertEqual(restore_quote(view,view,raw,edits),raw)
        quote='[UE](frozen/engineering/UE/x.cpp:12)'
        self.assertIn(restore_quote(quote,view,raw,edits),raw)

    def test_packet_seal_has_no_history_and_detects_tampering(self):
        # Fixture gold is synthetic; no historical scoring records enter the packet.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';(source/'input').mkdir(parents=True)
            (source/'input/question.txt').write_text('question',encoding='utf-8')
            (source/'input/prompt.txt').write_text('prompt',encoding='utf-8')
            answer=root/'answer.txt';answer.write_text('correct supporting evidence',encoding='utf-8')
            gold=formal_v3_gold(v4_gold_from_v3(v3_gold({'reference_facts':['fixture'],'verification':['fixture']})))
            rubric=root/'rubric.json';core.write(rubric,gold)
            packet=root/'packet';private=root/'private.json'
            prepare_topic([{'identity':'one','source':str(source),'answer':str(answer),'answer_sha256':core.sha(answer)}],rubric,packet,private)
            self.assertFalse((packet/'private.json').exists())
            label='A001';score={
                'points':{r['id']:r['max'] for r in gold['rubric']},
                'notes':{r['id']:'Reviewed supporting evidence.' for r in gold['rubric']},
                'evidence':{r['id']:['correct supporting evidence'] for r in gold['rubric']},'issues':[],
                'checks':{c['id']:{'status':'correct','note':'Reviewed.','evidence':['correct supporting evidence']} for c in gold['checkpoints']},
                'presentation_notes':[]}
            core.write(packet/'reviews'/f'{label}.json',score)
            completed=subprocess.run([sys.executable,'-X','utf8',str(packet/'review_io.py'),'seal'],capture_output=True,text=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual(check_topic_seal(packet)['answers'],1)
            with self.assertRaisesRegex(ValueError,'external input seal'):check_topic_seal(packet,'different')
            self.assertNotIn('old_judgment',core.read(packet/'scores.json')[label])
            (packet/'answers'/f'{label}.txt').write_text('changed',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'input changed'):check_topic_seal(packet)

    def test_history_attachment_does_not_allow_rewriting_independent_judgment(self):
        trial=v3_gold({'reference_facts':['fixture'],'verification':['fixture']})
        gold=formal_v3_gold(v4_gold_from_v3(trial))
        old=v4_score_from_v3(sample_score(),trial)
        score=copy.deepcopy(old)
        provenance={'review_protocol':'fresh-topic-blind','history_revealed_after_all_topic_seals':True,
            **{key:'sealed' for key in ('topic_seal_sha256','input_seal_sha256','view_score_sha256','all_topics_seal_sha256')},
            'original_judgment_sha256':core.digest(score)}
        score.update(review_revision='r1',review_audit={'revision':'r1','pre_review_quality':core.principle_score(old,gold)['quality'],
            'old_judgment':old,'old_issue_dispositions':[],'whole_answer_reviewed':True,'first_pass_seal_sha256':'sealed',
            'criterion_changes':{cid:{'before':v,'after':v,'delta':0,'reason':'Independent item review.'} for cid,v in score['points'].items()},
            'fresh_evaluation':provenance})
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'evaluation/review-source'
            core.write(base/'grading/scores.json',{'old':old})
            core.write(base/'grading/private-mapping.json',{'old':'result.json'})
            core.write(base/'rubric.json',gold)
            core.write(root/'evaluation/manifest.json',{'review_revision':'r1','review_protocol':'fresh-topic-blind'})
            validate_review_scores(root,{'fresh':'result.json'},{'fresh':score},gold)
            score['review_notes']=['Changed after seal']
            with self.assertRaisesRegex(ValueError,'judgment changed'):validate_review_scores(root,{'fresh':'result.json'},{'fresh':score},gold)


if __name__=='__main__':unittest.main()
