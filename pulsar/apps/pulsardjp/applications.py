from datetime import datetime

import djpcms
from djpcms import forms, views, html, ajax
from djpcms.template import loader
from djpcms.apps.included import admin
from djpcms.utils.dates import nicetimedelta, smart_time
from djpcms.utils.text import nicename
from djpcms.utils import mark_safe
from djpcms.forms.utils import return_form_errors, get_form, saveform

from stdnet import orm

import pulsar
from pulsar.utils.py2py3 import iteritems
from pulsar.apps import rpc

from .models import Task, JobModel
from .forms import get_job_form, ServerForm


monitor_template = '''\
<div class="yui-g">
    <div class="yui-u first">
        <div class="pulsar-panel">{% for panel in left_panels %}
         <div class="flat-panel">
          <div class="hd">
           <h2>{{ panel.name }}</h2>
          </div>
          <div class="bd">
           {{ panel.value }}
          </div>
         </div>{% endfor %}
        </div>
    </div>
    <div class="yui-u"> 
        <div class="pulsar-panel">{% for panel in right_panels %}
         <div class="flat-panel">
          <div class="hd">
           <h2>{{ panel.name }}</h2>
          </div>
          <div class="bd">
           {{ panel.value }}
          </div>
         </div>{% endfor %}
        </div>
    </div>
</div>'''
    

class ServerView(admin.TabView):
    converters = {'uptime': nicetimedelta,
                  'last_notified': smart_time,
                  'default_timeout': nicetimedelta,
                  'timeout': nicetimedelta}
    
    def get_client(self, instance):
        return rpc.JsonProxy(instance.path)
        
    def render_object_view(self, djp, appmodel, instance):
        r = self.get_client(instance)
        try:
            return self.get_panels(djp,appmodel,instance,r.server_info())
        except pulsar.ConnectionError:
            return 'No connection'
    
    def pannel_data(self, data):
        for k,v in iteritems(data):
            if k in self.converters:
                v = self.converters[k](v)
            yield (nicename(k),v)
    
    def workers_panel(self, djp, workers):
        for worker in workers:
            aid = worker['aid']
            dl = html.DefinitionList(data_stream = self.pannel_data(worker),
                                     cn = 'object-definition')
            yield aid,dl.render(djp)
            
    def get_panels(self,djp,appmodel,instance,info):
        server = html.DefinitionList(\
                    data_stream = self.pannel_data(info['server']),
                    cn = 'object-definition')
        data_stream = [('Server',server.render(djp))]
        for monitor in info['monitors']:
            workers = monitor.pop('workers',None)
            tabs = html.tabs(data_stream = self.workers_panel(djp,workers))
            data_stream.append((monitor['name'],tabs.render(djp)))
        return html.tabs(data_stream = data_stream).render(djp)


class PulsarView(views.ViewView):
    
    def default_post(self, djp):
        '''Get response handler.'''
        raise NotImplementedError('Post response not implemented')

    
class PulsarServerApplication(admin.AdminApplication):
    inherit = True
    form = ServerForm
    list_per_page = 100
    converters = {'uptime': nicetimedelta}
    list_display = ('code','path','this','notes')
    object_widgets = views.extend_widgets({'home':ServerView()})
    
    view = PulsarView()
    
    def get_client(self, instance):
        return rpc.JsonProxy(instance.path)
    
     

################################    TASKQUEUE DJPCMS APPLICATION

task_display = ('job','status','timeout','time_executed',
    'time_start','time_end',
    html.table_header('task_duration','duration',function='duration'),
    'expiry',
    'api',
    'user')


class JobsView(views.SearchView):
    astable = True
     
    def linkname(self, djp):
        return 'Job list'
    
    def title(self, djp):
        try:
            p = self.appmodel.proxy(djp.request)
            return 'Job list on {0}'.format(p.domain)
        except:
            return 'No Jobs'
        

class JobRun(views.ViewView):
    '''A view for running task on demand from a web page.'''
    def default_post(self, djp):
        return saveform(djp, force_redirect = False)
        
    def get_form(self, djp, **kwargs):
        instance= djp.instance
        form = get_job_form(instance)
        if not isinstance(form,forms.HtmlForm):
            form = forms.HtmlForm(form,inputs = (('run','run'),))
        return get_form(djp,form).addClass(instance.id)
    
    def save(self, djp, f, commit = True):
        kwargs = f.cleaned_data
        instance = djp.instance
        p = self.appmodel.proxy(djp.request)
        res = self.appmodel.run(p, instance.id, **kwargs)
        if res:
            url = djp.site.get_url(res, request = djp.request)
            link = html.Widget('a',href=url).render(inner=res)\
                             if url else str(res)
            f.add_message(link+' sent to taskqueue')
        else:
            f.add_error('Could not create task')
        return f


class JobDisplay(html.ObjectItem):
    tag = 'div'
    default_class = 'yui3-g'
    _body = '''<p>{0}</p>\n{1}'''
    _inner_template = '''\
<div class="yui3-u-1-3">
    {0[inner]}
</div>
<div class="yui3-u-2-3">
    {0[tasks]}
</div>'''
    
    def stream(self, djp, widget, context):
        instance=  context['instance']
        df = self.definition_list(djp,context)
        view = context['view']
        if view:
            vdjp = view['view']
            form = vdjp.view.get_form(vdjp).render(vdjp)
        else:
            form = ''
        bd = self._body.format(instance.doc,df.render(djp))
        inner = html.box(instance.name, bd, form)
        qs = instance.tasks()
        app = djp.site.for_model(Task,all=True)
        tasks = app.render_query(app.root_view(djp.request, **djp.kwargs),qs)
        yield self._inner_template.format({'inner':inner,
                                           'tasks':tasks})


class JobApplication(views.ModelApplication):
    '''An application for running jobs from a web page.'''
    proxy = None
    list_display = ('name','type','next_run','run_every','runs_count')
    object_display = ('id','type','next_run','run_every','runs_count')
    table_actions = [views.application_action('bulk_run','run', djpcms.ADD)]
    search = JobsView()
    view = JobRun(regex = '(?P<id>{0})'.format(views.SLUG_REGEX))
    task_header = ('name','status','user','time_executed','id')
    object_widgets = {'home':JobDisplay()}
    
    def basequery(self, djp):
        p = self.proxy(djp.request)
        try:
            jobs = p.job_list()
        except:
            return 'No connection'
        return sorted((JobModel(name,data,p) for\
                       name,data in jobs),key = lambda x : x.name)
        
    def run(self, p, job, **kwargs):
        res = p.run_new_task(jobname = job, **kwargs)
        if 'id' in res:
            return Task.objects.get(id = res['id'])
        
    def get_object(self, request, **kwargs):
        if len(self.model_url_bits) != 1:
            return None
        model_id_url = self.model_url_bits[0]
        if not model_id_url in kwargs:
            return None
        id = kwargs[model_id_url]
        if isinstance(id,self.model):
            return id
        else:
            p = self.proxy(request)
            try:
                job = p.job_list(jobnames = (id,))
            except:
                return None
            if job:
                job = job[0][1]
                return JobModel(id,job,p)
        
    def ajax__bulk_run(self, djp):
        request = djp.request
        data = request.REQUEST
        if 'ids[]' in data:
            taskapp = djp.site.for_model(Task)
            p = self.proxy(djp.request)
            body = []
            for job in data.getlist('ids[]'):
                try:
                    task = p.run_new_task(job)
                except:
                    continue
                if task:
                    instance=  Task.objects.get(id = task['id'])
                    url = taskapp.viewurl(request,instance)
                    task['time_executed'] = smart_time(task['time_executed'])
                    if url:
                        task['id'] = html.Widget('a',href=url)\
                                            .render(inner=instance.id)
                body.append([task.get(head) for head in self.task_header])
            if not body:
                inner = html.Table(self.task_header,
                                   body = body,
                                   footer = False,
                                   data = {'options':{'sDom':'t'}}).render(djp)
            else:
                inner = '<p>Nothing done.</p>'
            return ajax.dialog(hd = 'Executed Tasks', bd = inner,
                               modal = True,
                               width = 700)
    
    
class TasksAdmin(admin.AdminApplicationSimple):
    list_display = (html.table_header('short_id', 'TID', function='id'),)\
                    + task_display
    list_display_links = ('id','short_id','job')
    object_display = ('id',) + task_display +\
                     ('string_result','logs') 
    has_plugins = False
    inherit = True
    proxy = None
    
    view = views.ViewView(regex = views.UUID_REGEX)

#
# Scripts
#

script_languages = (
                    ('python','python'),
                    )


class ScriptForm(forms.Form):
    name = forms.CharField(toslug = '_')
    language = forms.ChoiceField(choices = script_languages)
    body = forms.CharField(widget = html.TextArea(
                                            default_class = 'taboverride'))
    
    def clean_name(self, value):
        return orm.test_unique('name',self.model,value,self.instance,
                               forms.ValidationError)
    

class RunScriptForm(forms.Form):
    parameters = forms.CharField(required = False)
    
    def clean_parameters(self, value):
        return value
    

class RunScriptView(views.ChangeView):
    
    def default_post(self, djp):
        fhtml = self.get_form(djp)
        form = fhtml.form
        if form.is_valid():
            self.appmodel.run(djp)
        else:
            return return_form_errors(fhtml,djp)
    

HtmlRunScriptForm = forms.HtmlForm(
    RunScriptForm,
    #layout = Layout(default_style = blockLabels2),
    inputs = (('run','_save'),)
)
    
    
class ScriptApplication(views.ModelApplication):
    inherit = True
    form = ScriptForm
    list_display = ('name','language','parameters')
    
    run_view = RunScriptView(regex = 'run', form = HtmlRunScriptForm)
    
    class Media:
        js = ['djpcms/taboverride.js']
    
    def run(self, djp, **params):
        '''This needs to be implemented by your application'''
        pass
        