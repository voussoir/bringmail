import flask
from flask import request

from voussoirkit import flasktools

from .. import common

site = common.site


@site.route('/favicon.ico')
@site.route('/favicon.png')
@site.route('/favicon.svg')
def favicon():
    return flask.send_file(
        common.STATIC_DIR.with_child('favicon.svg').absolute_path,
        mimetype='image/svg+xml',
    )


@site.route('/')
@site.route('/all')
@site.route('/account/<int:account_id>')
@site.route('/folder/<int:folder_id>')
def get_root(account_id=None, folder_id=None):
    if account_id is not None:
        common.get_account(account_id, response_type='html')
    if folder_id is not None:
        common.get_folder(folder_id, response_type='html')
    return common.render_template(request, 'root.html')


@site.route('/about')
def get_about():
    return common.render_template(request, 'about.html')


@site.route('/sse')
def get_sse():
    response = flask.Response(flasktools.sse_generator(), mimetype='text/event-stream')
    response.direct_passthrough = True
    return response


@site.route('/idle/status.json')
def get_idle_status():
    if common.supervisor is None:
        return flasktools.json_response({})
    return flasktools.json_response(common.supervisor.status())
