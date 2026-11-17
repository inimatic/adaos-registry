"""Explicit synthetic integration fixture. Never loads a real account vault."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from handlers.service import create_app
from handlers.profiles import descriptor


class FixtureAccount:
    def __init__(self,store): self.store=store
    def status(self): return {'connected':True,'plan_enabled':True,'binding_ref':'synthetic-only','generation':0,'email':'Synthetic provider'}
    def models(self,refresh=False): return [descriptor({'id':'fixture-reasoner','label':'Synthetic reasoner','capabilities':{'reasoning_efforts':['low','high'],'default_reasoning_effort':'low','web_search':True}}),descriptor({'id':'fixture-unknown','label':'Synthetic unknown'})]
    def stream(self,model,inputs,instructions,cancelled,**kwargs):
        if 'Обнови память' in inputs[-1]['content']:
            yield json.dumps({'summary':'Synthetic chat memory','preferences':'','agreements':'','source_ids':[]})
        else: yield '## Synthetic answer\n\n**Formatted** and $x^2$.\n\n| Item | Result |\n|---|---|\n| Review | Passed |'


if __name__=='__main__':
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--port',type=int,default=19085)
    args=parser.parse_args();data=Path(args.data).resolve()
    if '.tmp' not in data.parts: raise RuntimeError('Synthetic fixtures require repository scratch storage')
    app=create_app(data,FixtureAccount);app.state.store.set_setting('auto_memory',False)
    uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)
