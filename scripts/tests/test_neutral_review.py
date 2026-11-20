"""Fresh reviews preserve originals and require complete judgment-change provenance."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
from benchmark import parser
from comparison import compare
from evaluation import formal_v3_gold,formal_v3_score,regrade,v4_gold_from_v3,v4_score_from_v3
from reporting import blind,build_report,seal,validate
import test_comparison as fixtures
from test_comparison import snapshot
from test_scoring_v3 import add_issue,sample_score,v3_gold


class NeutralReviewTests(unittest.TestCase):
    setUp=fixtures.ComparisonTests.setUp
    tearDown=fixtures.ComparisonTests.tearDown
    grade=fixtures.ComparisonTests.grade
    run_fixture=fixtures.ComparisonTests.run_fixture

    def test_fresh_judgment_audit_and_both_comparison_baselines_survive_exports(self):
        source=self.run_fixture('original',tiers=['default'],efforts=['max'],version=2)
        trial=v3_gold(core.read(source/'input/gold.json'))
        formal=formal_v3_gold(v4_gold_from_v3(trial))
        rubric=self.base/'formal.json';core.write(rubric,formal)
        old=self.base/'old-v3';regrade(source,rubric,old);blind(old)
        old_score=formal_v3_score(v4_score_from_v3(add_issue(sample_score(),'error','local'),trial),v4_gold_from_v3(trial))
        old_scores={a:copy.deepcopy(old_score) for a in core.read(old/'grading/private-mapping.json')}
        path=self.base/'old-scores.json';core.write(path,old_scores);seal(old,path);old_summary=build_report(old)
        original_hashes,old_hashes=snapshot(source),snapshot(old)
        fresh=copy.deepcopy(formal);fresh['review_revision']='neutral-r1'
        core.write(rubric,fresh)
        target=self.base/'neutral'
        with patch('runtime.Server',side_effect=AssertionError('No sampling')):
            receipt=regrade(source,rubric,target,review_from=old,review_revision='neutral-r1')
        self.assertFalse(receipt['inference_started']);self.assertEqual(receipt['next'],'blind')
        blind(target);mapping=core.read(target/'grading/private-mapping.json')
        scores={}
        for anon in mapping:
            score=copy.deepcopy(old_score);score['issues']=[]
            for check in score['checks'].values():check['status']='correct'
            for item in fresh['rubric']:
                if item['dimension']=='accuracy':score['points'][item['id']]=item['max']
            score.update(review_revision='neutral-r1',review_audit={
                'revision':'neutral-r1','pre_review_quality':old_summary['rows'][0]['quality'],
                'old_judgment':old_score,'whole_answer_reviewed':True,'first_pass_seal_sha256':'fixture',
                'old_issue_dispositions':[{'old_index':0,'action':'withdrawn','reason':'Full context supports the conditional interpretation.'}],
                'criterion_changes':{cid:{'before':old_score['points'][cid],'after':value,
                    'delta':float(Decimal(str(value))-Decimal(str(old_score['points'][cid]))),'reason':'Reviewed full answer.'}
                    for cid,value in score['points'].items()}})
            scores[anon]=score
        path=self.base/'fresh-scores.json';core.write(path,scores)
        bad=copy.deepcopy(scores);next(iter(bad.values()))['review_audit']['old_issue_dispositions']=[]
        core.write(path,bad)
        with self.assertRaisesRegex(ValueError,'Every old issue'):seal(target,path)
        bad=copy.deepcopy(scores);next(iter(bad.values()))['review_audit']['pre_review_quality']=0
        core.write(path,bad)
        with self.assertRaisesRegex(ValueError,'pre-review quality'):seal(target,path)
        core.write(path,scores);seal(target,path);summary=build_report(target);validate(target)
        self.assertEqual(summary['previous_evaluation']['scoring_version'],2)
        self.assertEqual(summary['rows'][0]['pre_review_quality'],old_summary['rows'][0]['quality'])
        output=self.base/'compared';compare([target],output)
        data=core.read(output/'comparison-data.json');row=data['samples'][0]
        self.assertEqual(row['previous_scoring_version'],2)
        self.assertEqual(row['pre_review_quality'],old_summary['rows'][0]['quality'])
        self.assertEqual(row['review_quality_delta'],float(Decimal(str(row['quality']))-Decimal(str(row['pre_review_quality']))))
        for name in ('samples.csv','conditions.csv','compact.csv','question-overview.csv'):
            self.assertIn('pre_review_quality',(output/name).read_text(encoding='utf-8-sig').splitlines()[0])
        self.assertIn('复核前 V3',(output/'matrix.html').read_text(encoding='utf-8'))
        self.assertIn('review_audit',(output/'matrix.html').read_text(encoding='utf-8'))
        self.assertEqual(snapshot(source),original_hashes);self.assertEqual(snapshot(old),old_hashes)
        args=parser().parse_args(['regrade','--run-dir',str(source),'--rubric',str(rubric),'--output','other',
                                  '--review-from',str(old),'--review-revision','neutral-r1'])
        self.assertEqual(args.review_revision,'neutral-r1')
        altered=copy.deepcopy(fresh);altered['checkpoints'][0]['level']='local' if altered['checkpoints'][0]['level']!='local' else 'key'
        core.write(rubric,altered)
        with self.assertRaisesRegex(ValueError,'denominator'):
            regrade(source,rubric,self.base/'bad-denominator',review_from=old,review_revision='neutral-r1')

if __name__=='__main__':unittest.main()
