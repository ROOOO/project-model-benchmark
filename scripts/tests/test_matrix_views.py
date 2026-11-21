"""Matrix views share frozen values and retain original dimension scales."""
import copy
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compact import render_principle_matrix


class MatrixMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.inputs, self.dimensions, self.scripts = {}, [], 0
        self.dimension, self.field = None, None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input':
            self.inputs[attrs['id']] = attrs
        if tag == 'script':
            self.scripts += 1
        if tag == 'div' and 'data-dimension' in attrs:
            self.dimension = {'id':attrs['data-dimension']}
        if self.dimension is not None and tag in ('span','small'):
            self.field = attrs.get('class')

    def handle_data(self, data):
        if self.dimension is not None and self.field:
            self.dimension[self.field] = self.dimension.get(self.field, '') + data

    def handle_endtag(self, tag):
        if tag in ('span','small'):
            self.field = None
        if tag == 'div' and self.dimension is not None:
            self.dimensions.append(self.dimension)
            self.dimension = None


def matrix_fixture(history=True, four_dimensions=False):
    scales = {'accuracy':50,'coverage':20,'insight':10,'readability':20} if four_dimensions else {'accuracy':70,'coverage':20,'insight':10}
    topics, rows, cases, details = [], [], [], {}
    for index in range(2):
        topic = {'id':f'topic-{index}', 'title':'共享预载' if index == 0 else '模块生命周期',
                 'scoring_version':3, 'dimension_maxima':dict(scales)}
        row = {'question_id':topic['id'], 'condition_id':f'condition-{index}', 'model':'gpt-6.2-sol',
               'effort':'max','service_tier':'default','valid_n':5,'quality_mean':79,'delivery_s_median':120,
               'error_answers':1,'omission_answers':0,'accuracy_mean':48.04+index*.01 if four_dimensions else 68.04+index*.01,
               'coverage_mean':20-index*2,'insight_mean':7-index,'readability_mean':19}
        if history:
            row.update(previous_scoring_version=2, previous_quality_mean=92,
                       quality_delta_mean=-13, review_revision='fixture', pre_review_quality_mean=90, review_quality_delta_mean=-11)
        sample = {'anonymous_id':'A001','previous_quality':92 if history else None,
                  'quality_delta':-13 if history else None,'raw_quality':95.04,'principle_penalty':10,'quality_cap':79,
                  'quality':79,'weighted_error_rate':.1,'weighted_omission_rate':0,'principle_groups':[],
                  'attempt_identity':f'answer-{index}','answer_link':'answers/example.txt'}
        score = {'points':{'A':68.04},'notes':{'A':'Source-supported review.'},'checks':{},'issues':[],
                 'presentation_notes':[],'review_notes':[]}
        if history:
            score['review_audit'] = {'old_judgment':{'points':{'A':70}}}
        topics.append(topic); rows.append(row)
        cases.append({'conditions':[{'id':row['condition_id'],'model':row['model'],'effort':row['effort'],'rows':[sample]}]})
        details[sample['attempt_identity']] = {'scores':score}
    return {'topics':topics,'rows':rows}, {'cases':cases,'grading_details':details}


class MatrixViewTests(unittest.TestCase):
    def test_default_view_has_dimensions_and_separate_history_without_changing_scores(self):
        overview, data = matrix_fixture()
        before = copy.deepcopy((overview,data))
        html = render_principle_matrix(overview,data)
        parsed = MatrixMarkup(html)
        self.assertIn('checked',parsed.inputs['scores-current'])
        self.assertNotIn('checked',parsed.inputs['scores-history'])
        self.assertEqual(parsed.inputs['scores-current']['name'],parsed.inputs['scores-history']['name'])
        self.assertEqual(parsed.scripts,0)
        self.assertEqual(len(parsed.dimensions),9)
        self.assertEqual(parsed.dimensions[0],{'id':'accuracy','dimension-label':'准确','dimension-value':'68.0','dimension-maximum':'/70'})
        self.assertIn('class="score">79.00',html)
        self.assertIn('class="history-only">V2分',html)
        self.assertIn('class="history-only">差值',html)
        self.assertIn('class="history-audit"><summary>历史评分审计（含旧判断）',html)
        self.assertIn('最终分另受原则扣分和封顶影响',html)
        self.assertIn('href="compact.html"',html)
        self.assertIn('href="question-overview.html"',html)
        self.assertEqual((overview,data),before)

    def test_dimension_means_are_equal_topic_means_with_rounding_only_at_display(self):
        overview, data = matrix_fixture()
        means = MatrixMarkup(render_principle_matrix(overview,data)).dimensions[-3:]
        # 68.04 and 68.05 average to 68.045 -> 68.0, not the 68.1 from averaging rounded values.
        self.assertEqual([d['dimension-value'] for d in means],['68.0','19.0','6.5'])

    def test_missing_history_stays_unknown_and_does_not_borrow_the_first_topic_score(self):
        overview, data = matrix_fixture(history=False)
        html = render_principle_matrix(overview,data)
        self.assertIn('disabled',MatrixMarkup(html).inputs['scores-history'])
        self.assertNotIn('<small class="previous">',html)
        overview, data = matrix_fixture()
        overview['rows'][1].update(previous_quality_mean=None,pre_review_quality_mean=None)
        html = render_principle_matrix(overview,data)
        summary = html.split('data-summary="mean"',1)[1].split('</tr>',1)[0]
        self.assertIn('V2 —',summary)
        self.assertIn('复核前 V3 —',summary)
        self.assertNotIn('V2 92',summary)

    def test_historical_dimensions_keep_their_scales_and_mixed_scales_are_not_averaged(self):
        overview, data = matrix_fixture(four_dimensions=True)
        dimensions = MatrixMarkup(render_principle_matrix(overview,data)).dimensions
        self.assertEqual(len(dimensions),12)
        self.assertEqual([d['dimension-maximum'] for d in dimensions[:4]],['/50','/20','/10','/20'])
        self.assertEqual(dimensions[3]['dimension-label'],'表达')
        overview['topics'][1]['dimension_maxima']['accuracy'] = 70
        html = render_principle_matrix(overview,data)
        self.assertIn('维度量表不同，见各题',html)


if __name__ == '__main__':
    unittest.main()
