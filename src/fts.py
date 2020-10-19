import os
import sys
import time
import subprocess
import atexit
import shutil
import string
import random
import pathlib
import time
import src.internals.graph as graphviz
from multiprocessing import Process, Queue
from src.internals.disambiguator import Disambiguator
from src.internals.analyser import z3_analyse_hdead, z3_analyse_full, load_dot
from src.internals.process_manager import ProcessManager
from flask import session, Flask, request, render_template
from werkzeug.utils import secure_filename

from src.internals.translator import Translator
from src.internals.vmc_controller import VmcController

UPLOAD_FOLDER = os.path.relpath("uploads")
ALLOWED_EXTENSIONS = {'dot'}
PATH_TO_VMC = './vmc65-linux'
vmc = None #It will host VmcController

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = b'\xb1\xa8\xc0W\x0c\xb3M\xd6\xa0\xf4\xabSmz=\x83'

def is_fts(file_path):
    """Check if the given file_path refers to an dot file containing an FTS.
    Return True on success, False otherwise"""
    with open(file_path, 'r') as source:
        try:
            load_dot(source)
            return True
        except:
            return False

def delete_output_file():
    if 'output' in session:
        try:
            os.remove(session['output'])
        except:
            pass
        try:
            os.remove(session['graph'])
        except:
            pass

def full_analysis_worker(fts_file, out_file, out_graph, queue):
    dead = [] 
    false = [] 
    hidden = []
    fts_source = open(fts_file, 'r')
    sys.stdout = open(out_file, 'w')
    fts = load_dot(fts_source)
    z3_analyse_full(fts)
    for transition in fts._set_dead:
        dead.append({'src':transition._in._id, 'dst':transition._out._id,
            'label':str(transition._label), 'constraint':str(transition._constraint)})
    for transition in fts._set_false_optional:
        false.append({'src':transition._in._id, 'dst':transition._out._id,
            'label':str(transition._label), 'constraint':str(transition._constraint)})
    for state in fts._set_hidden_deadlock:
        hidden.append(state._id)
    queue.put({'ambiguities':{'dead': dead, 'false': false, 'hidden': hidden}})
    fts.report()
    sys.stdout.close()
    fts_source.close()

def hdead_analysis_worker(fts_file, out_file, out_graph, queue):
    hidden = []
    fts_source = open(fts_file, 'r')
    sys.stdout = open(out_file, 'w')
    fts = load_dot(fts_source)
    z3_analyse_hdead(fts)
    for state in fts._set_hidden_deadlock:
        hidden.append(state._id)
    queue.put({'ambiguities':{'dead':[], 'false':[], 'hidden': hidden}})
    fts.report()
    sys.stdout.close()
    fts_source.close()

def check_session():
    if ('timeout' in session and session['timeout'] is not None 
            and session['timeout'] > time.time()):
        return True
    return False

@app.route('/keep_alive', methods=['POST'])
def update_session_timeout():
    tmp = ['output', 'graph', 'model']
    if check_session():
        session['timeout'] = time.time()+600
        for target in tmp:
            if target in session and os.path.isfile(session[target]):
                try:
                    pathlib.Path(session[target]).touch()
                except:
                    pass
    return {'text':'ok'}, 200

def new_session():
    if 'output' in session and session['output']:
        delete_output_file()
    now = time.time()
    session['position'] = 0
    session['timeout'] = now+600
    session['output'] = ''.join(random.SystemRandom().choice(
                string.ascii_uppercase + string.digits) for _ in range(32))
    session['graph'] = os.path.join('src', 'static', session['output']+'.svg')
    session['counter_graph'] = os.path.join('src', 'static', session['output'] + '_COUNTEREXAMPLE.svg')
    session['model'] = os.path.join(UPLOAD_FOLDER, session['output']+'.dot')
    session['output'] = os.path.join('tmp', session['output']+'-output')
    session['ambiguities'] = {}
    
def close_session():
    pm = ProcessManager.get_instance()
    session.pop('timeout', None)
    session.pop('position', None)
    if 'id' in session and session['id']:
        pm.end_process(session['id'])
    delete_output_file()
    session.pop('id', None)
    session.pop('output', None)
    session.pop('ambiguities', None)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/yield')
def get_output():
    pm = ProcessManager.get_instance()
    if not check_session():
        delete_output_file()
        return {"text":'\nSession timed-out'}, 404
    elif not 'id' in session or not pm.process_exists(session['id']):
        delete_output_file()
        return {"text":''}, 404
    else:
        with open(session['output']) as out:
            if pm.is_alive(session['id']):
                out.seek(session['position'])
                result = out.read(4096)
                session['position'] = out.tell()
                return {"text":result}, 206
            else:
                out.seek(session['position'])
                result = out.read()
                os.remove(session['output'])
                queue = ProcessManager.get_instance().get_queue(session['id'])
                payload = {}
                payload['text'] = result
                graph = graphviz.Graph.from_file(session['model'])
                payload['edges'], payload['nodes'] = graph.get_graph_number()
                payload['mts'] = graph.draw_mts()
                if(queue):
                    tmp = queue.get()
                    session['ambiguities'] = tmp['ambiguities']
                    payload['ambiguities'] = tmp['ambiguities']
                    ProcessManager.get_instance().delete_queue(session['id'])
                    try:
                        dis = Disambiguator.from_file(session['model'])
                        dis.highlight_ambiguities(tmp['ambiguities']['dead'], 
                                tmp['ambiguities']['false'], 
                                tmp['ambiguities']['hidden'])
                        payload['graph'] = dis.get_graph()
                        graphviz.Graph(dis.get_graph()).draw_graph(session['graph'])
                        return payload, 200
                    except:
                        return payload, 200
                return payload, 200

@app.route('/upload', methods=['POST'])
def upload_file():
    payload = {}
    dot = ""
    close_session()
    new_session()
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER);
    if not os.path.exists(os.path.dirname(session['output'])):
        os.makedirs(os.path.dirname(session['output']))
    if request.method == 'POST':
        # check if the post request has the file part
        if 'file' not in request.files:
            return {"text":'No file part'}, 400
        file = request.files['file']
        # if user does not select file, browser also
        # submit an empty part without filename
        if file.filename == '':
            return {"text": 'No selected file'}, 400
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            if(filename.split(".")[-1].lower() == "dot"):
                file_path = session['model']
                file.save(file_path)
                if(not is_fts(file_path)):
                    os.remove(file_path)
                    return {"text":"The given file is not a FTS or contains errors"}, 400
                with open(file_path, 'r') as source:
                    dot = source.read()
                graph = graphviz.Graph(dot)
                payload['mts'] = graph.draw_mts()
                graph.draw_graph(session['graph'])
                payload['graph'] = dot
                payload['edges'], payload['nodes'] = graph.get_graph_number()
                payload['text'] = "Model loaded"
                return payload, 200
        else:
            return {"text": "Incompatible file format"}, 400
    return {"text": "Invalid request"}, 400

@app.route('/', methods=['GET', 'POST'])
def index():
    return render_template("main.html")

@app.route('/full_analysis', methods=['POST'])
def full_analyser():
    pm = ProcessManager.get_instance()
    queue = Queue()
    update_session_timeout()
    file_path = session['model']
    if os.path.isfile(file_path):
        thread = Process(target=full_analysis_worker,
                args=[file_path, session['output'], session['graph'], queue])
        session['id'] = str(thread.name)
        pm.add_process(session['id'], thread)
        pm.add_queue(session['id'], queue)
        pm.start_process(session['id'])
        session['position'] = 0
        return "Processing data..."
    return {"text": 'File not found'}, 400

@app.route('/hdead_analysis', methods=['POST'])
def hdead_analyser():
    pm = ProcessManager.get_instance()
    queue = Queue()
    update_session_timeout()
    file_path = session['model']
    if os.path.isfile(file_path):
        thread = Process(target=hdead_analysis_worker,
                args=[file_path, session['output'], session['graph'], queue])
        session['id'] = str(thread.name)
        pm.add_process(key=session['id'], process=thread)
        pm.add_queue(session['id'], queue)
        pm.start_process(session['id'])
        session['position'] = 0
        return "Processing data..."
    return {"text": 'File not found'}, 400

@app.route('/delete_model', methods=['POST'])
def delete_model():
    file_path = session['model']
    close_session()
    try:
        os.remove(file_path)
    except OSError as e:
        return {"text": "An error occured while deleting the file model"}, 400
    else:
        return {"text":"Model file deleted"}, 200

@app.route('/stop', methods=['POST'])
def stop_process():
    pm = ProcessManager.get_instance()
    session.pop('position', None)
    if 'id' in session and session['id']:
        pm.end_process(session['id'])
    delete_output_file()
    session.pop('id', None)
    session.pop('ambiguities', None)
    session.pop('ambiguities', None)
    return {"text":'Stopped process'}, 200

@app.route('/remove_ambiguities', methods=['POST'])
def disambiguate():
    pm = ProcessManager.get_instance()
    if not check_session():
        return {"text": "No ambiguities data available execute a full analysis first"}, 400
    if not session['ambiguities']:
        queue = pm.get_queue(session['id'])
        if not queue:
            return {"text": "No ambiguities data available execute a full analysis first"}, 400
        else: 
            session['ambiguities'] = queue.get()
    payload = {}
    file_path = session['model']
    if os.path.isfile(file_path):
        dis = Disambiguator.from_file(file_path)
        dis.remove_transitions(session['ambiguities']['dead'])
        dis.set_true_list(session['ambiguities']['false'])
        dis.solve_hidden_deadlocks(session['ambiguities']['hidden'])
        pm.delete_queue(session['id'])
        graph = graphviz.Graph(dis.get_graph())
        payload['mts'] = graph.draw_mts()
        payload['text'] = "Removed ambiguities"
        payload['graph'] = graph.get_graph()
        payload['edges'], payload['nodes'] = graph.get_graph_number()
        graph.draw_graph(session['graph'])
        return payload, 200
    return {"text": 'File not found'}, 400

@app.route('/apply_all', methods=['POST'])
def apply_all():
    payload, status = disambiguate()
    if status == 200:
        if os.path.isfile(session['model']):
            with open(session['model'], 'w') as model:
                try:
                    model.write(payload['graph']);
                    session['ambiguities']['dead'] = []
                    session['ambiguities']['false'] = []
                    session['ambiguities']['hidden'] = []
                    return {'text': 'Model file updated correctly'}, 200
                except:
                    return {'text':'Unable to update file model'}, 400
    return {'text':'Unable to update file model'}, 400


@app.route('/remove_false_opt', methods=['POST'])
def solve_fopt():
    pm = ProcessManager.get_instance()
    if not check_session():
        return {"text": "No ambiguities data available execute a full analysis first"}, 400
    if not session['ambiguities']:
        queue = pm.get_queue(session['id'])
        if not queue:
            return {"text": "No ambiguities data available execute a full analysis first"}, 400
        else: 
            session['ambiguities'] = queue.get()
    payload = {}
    file_path = session['model']
    if os.path.isfile(file_path):
        dis = Disambiguator.from_file(file_path)
        dis.set_true_list(session['ambiguities']['false'])
        pm.delete_queue(session['id'])
        graph = graphviz.Graph(dis.get_graph())
        payload['mts'] = graph.draw_mts()
        payload['text'] = "Removed false optional transitions"
        payload['graph'] = graph.get_graph()
        payload['edges'], payload['nodes'] = graph.get_graph_number()
        graph.draw_graph(session['graph'])
        return payload, 200
    return {"text": 'File not found'}, 400

@app.route('/apply_fopt', methods=['POST'])
def apply_fopt():
    payload, status = solve_fopt()
    if status == 200:
        if os.path.isfile(session['model']):
            with open(session['model'], 'w') as model:
                try:
                    model.write(payload['graph']);
                    session['ambiguities']['false'] = []
                    return {'text': 'Model file updated correctly'}, 200
                except:
                    return {'text':'Unable to update file model'}, 400
    return {'text':'Unable to update file model'}, 400

@app.route('/remove_dead_hidden', methods=['POST'])
def solve_hdd():
    pm = ProcessManager.get_instance()
    if not check_session():
        return {"text": "No ambiguities data available execute a full analysis first"}, 400
    if not session['ambiguities']:
        queue = pm.get_queue(session['id'])
        if not queue:
            return {"text": "No ambiguities data available execute a full analysis first"}, 400
        else: 
            session['ambiguities'] = queue.get()
    payload = {}
    file_path = session['model']
    if os.path.isfile(file_path):
        dis = Disambiguator.from_file(file_path)
        dis.remove_transitions(session['ambiguities']['dead'])
        dis.solve_hidden_deadlocks(session['ambiguities']['hidden'])
        pm.delete_queue(session['id'])
        graph = graphviz.Graph(dis.get_graph())
        payload['mts'] = graph.draw_mts()
        payload['text'] = "Removed hidden deadlocks and dead transitions"
        payload['graph'] = graph.get_graph()
        payload['edges'], payload['nodes'] = graph.get_graph_number()
        graph.draw_graph(session['graph'])
        return payload, 200
    return {"text": 'File not found'}, 400

@app.route('/apply_hdd', methods=['POST'])
def apply_hdd():
    payload, status = solve_hdd()
    if status == 200:
        if os.path.isfile(session['model']):
            with open(session['model'], 'w') as model:
                try:
                    model.write(payload['graph']);
                    session['ambiguities']['dead'] = []
                    session['ambiguities']['hidden'] = []
                    return {'text': 'Model file updated correctly'}, 200
                except:
                    return {'text':'Unable to update file model'}, 400
    return {'text':'Unable to update file model'}, 400


@app.route('/verify_property', methods=['POST'])
def verify_property():
    pm = ProcessManager.get_instance()
    if not session['ambiguities']:
        queue = pm.get_queue(session['id'])
        if not queue:
            return {"text":"No ambiguities data available execute a full analysis first"}, 400
    if not (len(session['ambiguities']['hidden']) == 0):
        return {"text":"Hidden deadlocks detected. It is necessary to remove them before checking the property"}, 400

    fname = secure_filename(request.form['name'])
    #fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
    fpath = session['model']

    actl_property = request.form['property']
    if (len(actl_property) == 0):
        return {"text":'Missing property to be verified'}, 400

    if os.path.isfile(fpath):
        t = Translator()
        t.load_model(fpath)
        t.translate()

        vmc_string = t.get_output()
        
        session_tmp_folder = session['output'].split('-output')[0]
        try:
            os.mkdir(session_tmp_folder)
        except FileExistsError:
            #if the directory is already present continue
            pass

        session_tmp_model = os.path.join(session_tmp_folder, 'model.txt')
        session_tmp_properties = os.path.join(session_tmp_folder, 'properties.txt')

        vmc_file = open(session_tmp_model,"w+")
        vmc_file.write(vmc_string)
        vmc_file.close()
        prop_file = open(session_tmp_properties,"w+")
        prop_file.write(actl_property)
        prop_file.close()

        global vmc
        try:
            vmc = VmcController(PATH_TO_VMC)
            vmc.run_vmc(session_tmp_model,session_tmp_properties)
        except ValueError as ve:
            if str(ve) == 'Invalid vmc_path':
                shutil.rmtree(session_tmp_folder)
                return {"text": "Unable to locate VMC executable"}, 400
            if str(ve) == 'Invalid model file':
                shutil.rmtree(session_tmp_folder)
                return {"text": 'Invalid model file'}, 400
            if str(ve) == 'Invalid properties file':
                shutil.rmtree(session_tmp_folder)
                return {"text": 'Invalid properties file'}, 400
        except:
            shutil.rmtree(session_tmp_folder)
            return {'text': 'An error occured'}, 400
        result = vmc.get_output()
        shutil.rmtree(session_tmp_folder)
        return {"text": result}, 200
    return {"text": 'File not found'}, 400

@app.route('/explanation', methods=['POST'])
def show_explanation():
    global vmc
    if check_session():
        if vmc == None:
            return {"text": 'No translation has been performed'}, 400
        return {"text": vmc.get_explanation()}, 200
    return {"text": 'No translation has been performed'}, 400

@app.route('/graph', methods=['POST'])
def get_graph():
    message = """No graph data available, the graph may be too big render.
        You can render it locally by downloading the graph source code and use
        the following command: dot -Tsvg model.dot -o output.svg"""
    if check_session(): 
        if os.path.isfile(session['graph']):
            return {"source":os.path.join('static', os.path.basename(session['graph']))}, 200
    return {"text":message}, 400

@app.route('/reload_graph', methods=['POST'])
def reload_graph():
    message = """No graph data available, the graph may be too big render.
        You can render it locally by downloading the graph source code and use
        the following command: dot -Tsvg model.dot -o output.svg"""
    graphviz.Graph(request.form['src']).draw_graph(session['graph'])
    if check_session(): 
        if os.path.isfile(session['graph']):
            return {"source":os.path.join('static', os.path.basename(session['graph']))}, 200
    return {"text":message}, 400

def delete_old_file(fmt, timeout, path):
    for f in os.listdir(path):
        f = os.path.join(path, f)
        if f.split('.')[-1] == fmt:
            try:
                if os.stat(f).st_mtime + timeout < time.time():
                    os.remove(f)
            except:
                pass

def deleter():
    timeout = 900
    while True:
        time.sleep(timeout)
        delete_old_file('svg', timeout, os.path.join('src', 'static'))
        delete_old_file('dot', timeout, os.path.join('uploads'))
        delete_old_file('txt', timeout, os.path.join('tmp'))
        delete_old_file('html', timeout, os.path.join('tmp'))
        delete_old_file('dot', timeout, os.path.join('tmp'))

def start_deleter():
    pm = ProcessManager.get_instance()
    thread = Process(target=deleter, daemon=True)
    pm.add_process('deleter', thread)
    pm.start_process('deleter')

app.before_first_request(start_deleter)

def stop_deleter():
    delete_old_file('svg', 0, os.path.join('src', 'static'))
    delete_old_file('dot', 0, os.path.join('uploads'))
    delete_old_file('txt', 0, os.path.join('tmp'))
    delete_old_file('html', 0, os.path.join('tmp'))
    delete_old_file('dot', 0, os.path.join('tmp'))

atexit.register(stop_deleter)

@app.route('/download', methods=['POST'])
def download():
    if not check_session():
        return {'text':"Session timed-out"}, 400

    payload = 'empty'
    mimetype = ''
    format = ''
    if(request.form['target'] == 'source'): 
        payload = request.form['main']
        mimetype = 'text/plain'
        format = "model.dot"
    elif(request.form['target'] == 'summary'): 
        payload = request.form['main']
        mimetype = 'text/html'
        format = "summary.html"
    elif(request.form['target'] == 'graph'): 
        mime = 'image/svg+xml'
        format = "graph.svg"
        path = os.path.join('static', os.path.basename(session['graph']))
        if os.path.isfile(os.path.join('src', path)):
            return {"source":path, 'name':format}, 200
        else:
            return {"text":"File not found"}, 404
    elif(request.form['target'] == 'console'): 
        payload = request.form['main']
        mimetype = 'text/plain'
        format="log.txt"
    else:
        return {"text":"Invalid request"}, 400

    with open(session['output']+format, 'w') as tmp:
        tmp.write(payload)
        return {"source":os.path.join('tmp', os.path.basename(
            session['output']+format)), 'name':format}, 200

def clean_counterexample():
    global vmc
    counter = vmc.get_explanation()
    lines = counter.split('\n')
    clean_counter = ''
    is_false = False
    for line in lines:
        if "-->" in line:
            #if at least an occurrence of '-->' is found we can infer
            #that the formula was evaluated as FALSE
            is_false = True
            clean_counter = clean_counter + line + '\n'
    if is_false:
        return clean_counter
    else:
        return 'NO'
    

@app.route('/counter_graph', methods=['POST'])
def show_counter_graph():
    if check_session():
        if vmc == None:
            return {"text": 'No translation has been performed'}, 400
        t = Translator()
        clean_counter = clean_counterexample()
        if clean_counter == 'NO':
            return {"text": 'The formula is TRUE'}, 200
        t.load_mts(clean_counter)
        t.mts_to_dot(session['counter_graph']) 
        ret_val = session['counter_graph'].split('/',1)
        return {"graph": '/' + ret_val[1]}, 200
