"""Fixed depth labels stay source-bound without weakening metric validation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from test_report_agent import answer, example
from workflow_agents.reporting.narrative import expand, make_prompt, validate, write_narrative


class ThresholdLabelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.materials=example(nodes=('Junction_A',))
        self.prompt,self.facts=make_prompt(self.materials)

    def test_category_token_can_be_a_noun_phrase(self):
        token=self.prompt['materials']['surface']['threshold_labels']['attention']
        response=answer(self.prompt)
        response['sections']['surface']=[token+'主要分布在道路上。']
        validate(response,self.materials,self.facts,['Junction_A'])
        self.assertEqual(expand(response['sections']['surface'][0],self.facts),
                         '水深达到15厘米的区域主要分布在道路上。')
        self.assertEqual(self.prompt['fact_bindings'][token[2:-2]]['metric'],'depth_threshold_label')

    def test_bare_threshold_and_observation_numbers_still_rejected(self):
        for text in ('达到15厘米的积水分布在道路上。','最大水深达到15厘米。','积水面积为15平方米。'):
            with self.subTest(text=text):
                response=answer(self.prompt);response['sections']['surface']=[text]
                with self.assertRaisesRegex(ValueError,'Unbound numeric'):
                    validate(response,self.materials,self.facts,['Junction_A'])

    async def test_failed_category_literal_gets_actionable_retry(self):
        bad=answer(self.prompt);bad['sections']['surface']=['达到15厘米的积水分布在道路上。']
        good=answer(self.prompt)
        good['sections']['surface']=[self.prompt['materials']['surface']['threshold_labels']['attention']+'分布在道路上。']
        callback=AsyncMock(side_effect=[bad,good])
        with tempfile.TemporaryDirectory() as temporary:
            result,_=await write_narrative(self.materials,['Junction_A'],Path(temporary),callback)
            audit=json.loads((Path(temporary)/'narrative_attempt_1.json').read_text(encoding='utf-8'))
        self.assertEqual(callback.await_count,2)
        feedback=callback.call_args.args[0]['repair']
        self.assertEqual(feedback['surface_threshold_labels'],self.prompt['materials']['surface']['threshold_labels'])
        self.assertIn('surface_threshold_labels',feedback['instruction'])
        self.assertIn('Unbound numeric',audit['error'])
        self.assertEqual(result['sections']['surface'][-1],good['sections']['surface'][0])


if __name__=='__main__':unittest.main()
