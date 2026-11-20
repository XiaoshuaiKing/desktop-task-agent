from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core import session_manager as sessions
from core.skill_manager import load_skills
from tools.permissions import check_permission,check_args,get_required_params

class FoundationTests(unittest.TestCase):
    def test_persistent_sessions_and_user_isolation(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(sessions,'DB_PATH',Path(temp)/'agent.db'):
            sessions.init_db()
            a=sessions.load_session('a');a['content'].append({'role':'user','content':'only-a'});sessions.save_session('a',a)
            b=sessions.load_session('b')
            self.assertNotIn('only-a',str(b))
            self.assertIn('only-a',str(sessions.load_session('a')))
            sessions.delete_session('a')
            self.assertNotIn('only-a',str(sessions.load_session('a')))
    def test_skill_directory_is_project_local(self):
        self.assertIn('pytorch-training',[s['name'] for s in load_skills()])
    def test_unknown_role_cannot_execute_tools(self):
        self.assertFalse(check_permission('write_text_file','unknown'))
    def test_required_argument_check(self):
        required=get_required_params('open_website')
        self.assertTrue(required)
        self.assertFalse(check_args({},required,'open_website'))

if __name__=='__main__':unittest.main()
