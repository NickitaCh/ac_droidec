/* Calculation stays in this worker. Only a concrete allocation returns to the site. */
const runtimeRoot = new URL('/static/vendor/optimizer/', self.location.href).href;
let boot;
async function initialize() {
    importScripts(runtimeRoot + 'pyodide.js', runtimeRoot + 'highs.js');
    const [python, solver, pulpZip] = await Promise.all([
        loadPyodide({indexURL: runtimeRoot}),
        Module({locateFile: name => runtimeRoot + name}),
        fetch(runtimeRoot + 'pulp.zip').then(r => { if (!r.ok) throw Error('runtime'); return r.arrayBuffer(); })
    ]);
    python.unpackArchive(pulpZip, 'zip', {extractDir: '/home/pyodide'});
    self.solveBrowserLP = (model, seconds) => JSON.stringify(solver.solve(model, {
        time_limit: seconds, output_flag: false, threads: 1, random_seed: 0
    }));
    self.optimizerProgress = message => self.postMessage({type: 'progress', message});
    return python;
}
self.onmessage = async ({data}) => {
    try {
        self.postMessage({type: 'progress', message: 'Загружаю вычислитель…'});
        boot ||= initialize();
        const python = await boot;
        python.globals.set('browser_payload_json', JSON.stringify(data.payload));
        await python.runPythonAsync(data.source);
        self.postMessage({type: 'progress', message: 'Подбираю сборки и распределяю модули…'});
        await python.runPythonAsync(`
from js import solveBrowserLP, optimizerProgress
class BrowserSolver(pulp.LpSolver):
    def actualSolve(self, problem, **kwargs):
        optimizerProgress('Распределяю модули между персонажами…')
        path = '/tmp/mod-allocation.lp'
        problem.writeLP(path)
        with open(path) as stream:
            answer = json.loads(solveBrowserLP(stream.read(), self.timeLimit or 45))
        columns = answer.get('Columns') or {}
        values = {name: column.get('Primal') for name, column in columns.items()}
        for variable in problem.variables():
            variable.varValue = values.get(variable.name)
        proven = answer.get('Status') == 'Optimal'
        feasible = bool(values) and all(v is not None for v in values.values())
        problem.assignStatus(pulp.LpStatusOptimal if proven else pulp.LpStatusNotSolved,
                             pulp.LpSolutionOptimal if proven else pulp.LpSolutionIntegerFeasible if feasible else pulp.LpSolutionNoSolutionFound)
        return problem.status
pulp.PULP_CBC_CMD = BrowserSolver
payload = json.loads(browser_payload_json)
validate_inventory(payload['mods'])
_worker_search = search
_searched = set()
def search(*args, **kwargs):
    base = args[1]
    repeat = base in _searched
    _searched.add(base)
    name = payload['names'].get(base) or base
    total = len(set(payload['rows']) & set(payload['units']))
    optimizerProgress(('Уточняю сборку: ' if repeat else f'Подбираю {len(_searched)} из {total}: ') + name)
    return _worker_search(*args, **kwargs)
_worker_recommend = mod_recommendations.recommend
def _browser_recommend(context, base):
    optimizerProgress('Проверяю, что фармить: ' + (payload['names'].get(base) or base))
    return _worker_recommend(context, base)
mod_recommendations.recommend = _browser_recommend
calculated = calculate(StatCalc(payload['calculator']), payload['members'], payload['rows'], payload['units'],
                       payload['tiers'], payload['mods'], payload['names'], locked=payload['locked'],
                       relic_mode=payload['relic_mode'], turn_order=payload['turn_order'], deep_search=payload['deep_search'])
browser_result_json = json.dumps({'allocation': {r['base_id']: [m['id'] for m in r['used_mods']]
                                               for r in calculated[0] if r['owned']},
                                 'optimal': calculated[2],
                                 'recommendations': {r['base_id']: [t['recipes'] for t in r.get('recommendations', [])]
                                                     for r in calculated[0] if r['owned']},
                                 **({'verification': calculated} if payload.get('_verification') else {})})
`);
        self.postMessage({type: 'complete', result: JSON.parse(python.globals.get('browser_result_json'))});
        python.globals.delete('browser_payload_json');
        python.globals.delete('payload');
    } catch (error) {
        // Python tracebacks may contain local data; keep them out of telemetry and the UI.
        self.postMessage({type: 'error', message: 'Не удалось выполнить подбор в браузере. Обновите страницу и повторите расчёт.'});
    }
};
