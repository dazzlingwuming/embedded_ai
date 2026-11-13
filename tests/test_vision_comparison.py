"""Protect paired experiment accounting and prevent label/policy leakage."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('vision_comparison',ROOT/'scripts/compare-vision-policies.py')
comparison=importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary=ROOT/'.cache/tmp'
        temporary.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.folder=self.root/'data/result'
        self.folder.mkdir(parents=True)
        source=self.root/'pc_backend/vision_frames.py'
        source.parent.mkdir()
        source.write_text('unchanged fixture source',encoding='utf-8')
        self.patch=patch.object(comparison,'ROOT',self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def fixture(self):
        frames=[{'image_id':f'image-{i}','path':f'{i}.jpg','sha256':f'sha-{i}',
                 'received_monotonic':float(i)} for i in range(1,5)]
        labels={'reviewer':'independent_fixture_labels','blinded_to_policy':True,
            'annotations':[{'image_id':f'image-{i}', 'object':obj,'screen_state':state}
                for i,obj,state in [(1,'screen','light_page'),(2,'screen','light_page'),
                                   (3,'keyboard','not_applicable'),(4,'cup','not_applicable')]]}
        cases=[{'case_id':'a','latest':frames[0],'quality':frames[1],
                'latest_age_at_trigger_ms':0,'selector_ms':100},
               {'case_id':'b','latest':frames[2],'quality':frames[3],
                'latest_age_at_trigger_ms':0,'selector_ms':200}]
        comparison.save(self.folder/'annotations.json',labels)
        comparison.save(self.folder/'image-manifest.json',frames)
        comparison.save(self.folder/'cases.json',cases)
        comparison.save(self.folder/'experiment.json',{'selector_sha256':comparison.digest(self.root/'pc_backend/vision_frames.py')})
        plan={'annotations_sha256':comparison.digest(self.folder/'annotations.json'),
            'cases_sha256':comparison.digest(self.folder/'cases.json'), 'repeats':1,
            'model_alias':'fixture-model','planned_requests':4}
        comparison.save(self.folder/'run-plan.json',plan)
        rows=[]
        for case in cases:
            for policy in comparison.POLICIES:
                frame=case[policy]
                expected=next(item for item in labels['annotations'] if item['image_id']==frame['image_id'])
                rows.append({'trial_id':case['case_id']+'-'+policy,'repeat':1,
                    'case_id':case['case_id'],'policy':policy,'image_id':frame['image_id'],
                    'error':None,'prediction':{key:expected[key] for key in ('object','screen_state')},
                    'selector_ms':case['selector_ms'] if policy=='quality' else 0,
                    'first_token_ms':600,'model_total_ms':800,
                    'constructed_selection_plus_first_token_ms':600+(case['selector_ms'] if policy=='quality' else 0),
                    'request_count':1,'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110},
                    'response_model':'fixture-model'})
        self.write_rows(rows)
        return rows

    def write_rows(self,rows):
        (self.folder/'trials.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')

    def summary(self):
        with patch('builtins.print'):
            comparison.summarize(self.folder)
        return comparison.read(self.folder/'summary.json')

    def test_correct_description_of_old_frame_is_not_counted_as_correct_current_scene(self):
        self.fixture()
        result=self.summary()
        self.assertEqual(result['statistics']['quality']['frame_joint_accuracy']['percent'],100)
        self.assertEqual(result['statistics']['quality']['trigger_joint_accuracy']['percent'],50)
        self.assertEqual(result['statistics']['latest']['trigger_joint_accuracy']['percent'],100)
        self.assertEqual(result['policy_frame_label_disagreement'],{'count':1,'n':2})
        self.assertEqual(result['paired_current_scene']['latest_only_correct'],1)

    def test_failed_calls_stay_in_accuracy_denominator_and_missing_usage_is_visible(self):
        rows=self.fixture()
        rows[0].update(error='network failure',prediction={},usage=None)
        self.write_rows(rows)
        result=self.summary()['statistics']['latest']
        self.assertEqual(result['object_accuracy'],{'correct':1,'n':2,'percent':50})
        self.assertEqual(result['errors'],1)
        self.assertEqual(result['usage_reported_trials'],1)
        self.assertEqual(result['tokens']['prompt_tokens']['sum'],100)
        self.assertNotIn('能看到收益的一组', (self.folder/'report.html').read_text(encoding='utf-8'))

    def test_label_changes_after_run_seal_are_rejected(self):
        self.fixture()
        value=comparison.read(self.folder/'annotations.json')
        value['annotations'][0]['object']='other'
        comparison.save(self.folder/'annotations.json',value)
        with self.assertRaisesRegex(ValueError,'changed'):
            comparison.summarize(self.folder)

    def test_ambiguous_labels_exclude_same_cases_for_both_arms_only_in_secondary_metric(self):
        self.fixture()
        labels=comparison.read(self.folder/'annotations.json')
        labels['annotations'][-1]['ambiguous']=True
        comparison.save(self.folder/'annotations.json',labels)
        plan=comparison.read(self.folder/'run-plan.json')
        plan['annotations_sha256']=comparison.digest(self.folder/'annotations.json')
        comparison.save(self.folder/'run-plan.json',plan)
        result=self.summary()
        self.assertEqual(result['statistics']['latest']['object_accuracy']['n'],2)
        self.assertEqual(result['common_unambiguous_case_count'],1)
        for policy in comparison.POLICIES:
            self.assertEqual(result['common_unambiguous_trigger_accuracy'][policy]['n'],1)

    def test_duplicate_trials_cannot_inflate_sample_count(self):
        rows=self.fixture()
        self.write_rows(rows+[deepcopy(rows[0])])
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            comparison.summarize(self.folder)

    def test_pair_order_is_counterbalanced_and_never_contains_answers(self):
        cases=[{'case_id':'a','expected_answer':'must-stay-private'}, {'case_id':'b','expected_answer':'also-private'}]
        jobs=comparison.schedule(cases,2)
        self.assertEqual(len(jobs),8)
        self.assertNotIn('private',json.dumps(jobs))
        for case in cases:
            first=[job['policy'] for job in jobs if job['case_id']==case['case_id'] and job['pair_position']==1]
            self.assertEqual(set(first),set(comparison.POLICIES))

    def test_budget_rejects_before_settings_or_network(self):
        self.fixture()
        (self.folder/'trials.jsonl').unlink()
        with patch.object(comparison,'settings') as settings:
            with self.assertRaisesRegex(ValueError,'exceeds'):
                asyncio.run(comparison.evaluate(self.folder,2,1))
            settings.assert_not_called()

    def test_invalid_or_extra_model_fields_cannot_be_scored_as_correct(self):
        for value in ('not json','{"object":"cup","screen_state":"dark_page"}',
                      '{"object":"screen","screen_state":"not_applicable"}',
                      '{"object":"keyboard","screen_state":"not_applicable","expected":true}'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                comparison.parse_prediction(value)

    def test_latency_quantiles_and_missing_values_are_not_zero_filled(self):
        result=comparison.summarize_numbers([None,100,200,300,float('nan')])
        self.assertEqual(result,{'n':3,'mean':200,'median':200,'p95':300,'min':100,'max':300})

if __name__=='__main__':
    unittest.main()
