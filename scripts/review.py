"""Provenance and consistency checks for a fresh offline review of sealed answers."""
from pathlib import Path

from core import check_inputs, inside, principle_score, read, require, scoring_gold, sha, three_dimension_scoring


def prepare_review_source(source, review, gold, selected_slots):
    from reporting import idle, score_data, summarize
    source, review = Path(source), Path(review).resolve()
    protocol, reviewed = check_inputs(source), check_inputs(review)
    idle(review)
    require((review / 'grading/seal.json').is_file(), 'Review source must have sealed scores')
    sampling = read(review / 'evaluation/source-protocol.json') if reviewed.get('offline_regrade') else reviewed
    require(sampling == protocol, 'Review source belongs to a different sampling protocol')
    score_data(review)
    old_gold = scoring_gold(review)
    require(gold.get('scoring_version') == old_gold.get('scoring_version') == 3
            and three_dimension_scoring(gold) and three_dimension_scoring(old_gold), 'Neutral review requires formal three-dimension V3')
    require([(r['id'], r['dimension'], r['max']) for r in gold['rubric']]
            == [(r['id'], r['dimension'], r['max']) for r in old_gold['rubric']], 'Review must preserve base score weights and item identities')
    keys = ('id', 'level', 'error_criterion', 'omission_criterion')
    require([{k:c[k] for k in keys} for c in gold['checkpoints']]
            == [{k:c[k] for k in keys} for c in old_gold['checkpoints']]
            and gold['principle_policy'] == old_gold['principle_policy'], 'Review must preserve checkpoint IDs, denominator, homes and principle policy')
    summary = summarize(review)
    require({k:v for k,v in summary.items() if k != 'generated_at'}
            == {k:v for k,v in read(review / 'summary.json').items() if k != 'generated_at'}, 'Review source summary is stale')
    covered = set(read(review / 'grading/private-mapping.json').values())
    selected = [r for r in read(source / 'summary.json')['rows'] if r['slot_id'] in selected_slots]
    for row in selected:
        relative = str(Path(row['answer_link']).with_name('result.json')).replace('\\','/')
        require(relative in covered, 'Review source does not cover selected answer')
        for name in (relative, row['answer_link']):
            require(sha(inside(source,name)) == sha(inside(review,name)), 'Review source answer, slot or execution record differs')
    files = {name:'evaluation/review-source/'+name for name in
             ('protocol.json','preparation-seal.json','summary.json','grading/scores.json','grading/seal.json','grading/private-mapping.json')}
    files['evaluation/rubric.json' if reviewed.get('offline_regrade') else 'input/gold.json'] = 'evaluation/review-source/rubric.json'
    return review, files


def validate_review_scores(root, mapping, scores, gold):
    """A changed judgment is allowed, but every old issue and base change is accounted for."""
    from decimal import Decimal
    base = Path(root) / 'evaluation/review-source'
    old_scores, old_mapping = read(base/'grading/scores.json'), read(base/'grading/private-mapping.json')
    old_gold = read(base/'rubric.json')
    by_result = {relative:old_scores[anon] for anon,relative in old_mapping.items()}
    revision = read(Path(root)/'evaluation/manifest.json')['review_revision']
    for anon, relative in mapping.items():
        score, old = scores[anon], by_result[relative]
        audit = score.get('review_audit', {})
        require(score.get('review_revision') == revision and audit.get('revision') == revision, 'Missing review revision')
        require(audit.get('pre_review_quality') == principle_score(old,old_gold)['quality'], 'Incorrect pre-review quality')
        require(audit.get('old_judgment') == old, 'Review did not preserve the sealed old judgment')
        dispositions = audit.get('old_issue_dispositions', [])
        require(sorted(d.get('old_index') for d in dispositions) == list(range(len(old['issues']))), 'Every old issue needs exactly one disposition')
        require(all(d.get('action') in ('retained','revised','withdrawn') and d.get('reason') for d in dispositions), 'Review disposition needs action and reason')
        changes = audit.get('criterion_changes', {})
        require(set(changes) == set(score['points']), 'Review must account for every scoring item')
        for cid, value in changes.items():
            require(value.get('before') == old['points'][cid] and value.get('after') == score['points'][cid]
                    and value.get('reason'), 'Incorrect base-score change audit')
            require(Decimal(str(value['delta'])) == Decimal(str(value['after']))-Decimal(str(value['before'])), 'Incorrect base-score delta')
        require(audit.get('first_pass_seal_sha256') and audit.get('whole_answer_reviewed') is True, 'Missing first-pass review provenance')


def review_quality_by_answer(root):
    base=Path(root)/'evaluation/review-source'
    return {row['answer_link']:row.get('quality') for row in read(base/'summary.json')['rows']}
