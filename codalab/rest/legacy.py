"""
Legacy REST APIs moved from the codalab-worksheets Django REST server. 
"""
import base64
from cStringIO import StringIO
from datetime import datetime, timedelta
import json
import logging
from oauthlib.common import generate_token
import random
import shlex
import traceback

from bottle import (
  get,
  HTTPError,
  httplib,
  HTTPResponse,
  local,
  post,
  redirect,
  request,
  response,
)

from codalab.bundles import get_bundle_subclass
from codalab.client.local_bundle_client import LocalBundleClient
from codalab.client.remote_bundle_client import RemoteBundleClient
from codalab.common import UsageError
from codalab.lib import (
  bundle_cli,
  file_util,
  formatting,
  metadata_util,
  spec_util,
  worksheet_util,
  zip_util,
)
from codalab.lib.codalab_manager import CodaLabManager
from codalab.model.tables import GROUP_OBJECT_PERMISSION_ALL
from codalab.objects.oauth2 import OAuth2Token
from codalab.objects.permission import permission_str
from codalab.server.auth import LocalUserAuthHandler
from codalab.server.authenticated_plugin import AuthenticatedPlugin
from codalab.server.rpc_file_handle import RPCFileHandle


class BundleService(object):
    '''
    Adapts the LocalBundleClient for REST calls.
    '''
    # Maximum number of lines of files to show
    HEAD_MAX_LINES = 100

    def __init__(self):
        self.client = LocalBundleClient(
            'local', local.bundle_store, local.model,
            LocalUserAuthHandler(request.user, local.model), verbose=0)

    def get_bundle_info(self, uuid):
        bundle_info = self.client.get_bundle_info(uuid, True, True, True)

        if bundle_info is None:
            return None
        # Set permissions
        bundle_info['edit_permission'] = (bundle_info['permission'] == GROUP_OBJECT_PERMISSION_ALL)
        # Format permissions into strings
        bundle_info['permission_str'] = permission_str(bundle_info['permission'])
        for group_permission in bundle_info['group_permissions']:
            group_permission['permission_str'] = permission_str(group_permission['permission'])

        metadata = bundle_info['metadata']

        cls = get_bundle_subclass(bundle_info['bundle_type'])
        for key, value in worksheet_util.get_formatted_metadata(cls, metadata):
            metadata[key] = value

        bundle_info['metadata'] = metadata
        bundle_info['editable_metadata_fields'] = worksheet_util.get_editable_metadata_fields(cls, metadata)

        return bundle_info

    def head_target(self, target, max_num_lines=HEAD_MAX_LINES):
        return self.client.head_target(target, max_num_lines)

    def search_worksheets(self, keywords, worksheet_uuid=None):
        return self.client.search_worksheets(keywords)

    def get_worksheet_uuid(self, spec):
        # generic function sometimes get uuid already just return it.
        if spec_util.UUID_REGEX.match(spec):
            return spec
        else:
            return worksheet_util.get_worksheet_uuid(self.client, None, spec)

    def full_worksheet(self, uuid):
        """
        Return information about a worksheet. Calls
        - get_worksheet_info: get basic info
        - resolve_interpreted_items: get more information about a worksheet.
        In the future, for large worksheets, might want to break this up so
        that we can render something basic.
        """
        worksheet_info = self.client.get_worksheet_info(uuid, True, True)

        # Fetch items.
        worksheet_info['raw'] = worksheet_util.get_worksheet_lines(worksheet_info)

        # Set permissions
        worksheet_info['edit_permission'] = (worksheet_info['permission'] == GROUP_OBJECT_PERMISSION_ALL)
        # Format permissions into strings
        worksheet_info['permission_str'] = permission_str(worksheet_info['permission'])
        for group_permission in worksheet_info['group_permissions']:
            group_permission['permission_str'] = permission_str(group_permission['permission'])

        # Go and fetch more information about the worksheet contents by
        # resolving the interpreted items.
        try:
            interpreted_items = worksheet_util.interpret_items(
                                worksheet_util.get_default_schemas(),
                                worksheet_info['items'])
        except UsageError, e:
            interpreted_items = {'items': []}
            worksheet_info['error'] = str(e)

        worksheet_info['items'] = self.client.resolve_interpreted_items(interpreted_items['items'])
        worksheet_info['raw_to_interpreted'] = interpreted_items['raw_to_interpreted']
        worksheet_info['interpreted_to_raw'] = interpreted_items['interpreted_to_raw']

        def decode_lines(interpreted):
            # interpreted is None or list of base64 encoded lines
            if interpreted is None:
                return formatting.contents_str(None)
            else:
                return map(base64.b64decode, interpreted)

        # Currently, only certain fields are base64 encoded.
        for item in worksheet_info['items']:
            if item['mode'] in ['html', 'contents']:
                item['interpreted'] = decode_lines(item['interpreted'])
            elif item['mode'] == 'table':
                for row_map in item['interpreted'][1]:
                    for k, v in row_map.iteritems():
                        if v is None:
                            row_map[k] = formatting.contents_str(v)
            elif 'bundle_info' in item:
                infos = []
                if isinstance(item['bundle_info'], list):
                    infos = item['bundle_info']
                elif isinstance(item['bundle_info'], dict):
                    infos = [item['bundle_info']]
                for bundle_info in infos:
                    try:
                        if isinstance(bundle_info, dict):
                            worksheet_util.format_metadata(bundle_info.get('metadata'))
                    except Exception, e:
                        print e
                        import ipdb; ipdb.set_trace()

        return worksheet_info

    def parse_and_update_worksheet(self, uuid, lines):
        """
        Replace worksheet |uuid| with the raw contents given by |lines|.
        """
        worksheet_info = self.client.get_worksheet_info(uuid, True)
        new_items, commands = worksheet_util.parse_worksheet_form(lines, self.client, worksheet_info['uuid'])
        self.client.update_worksheet_items(worksheet_info, new_items)
        # Note: commands are ignored

    def get_bundle_contents(self, uuid):
        """
        If bundle is a single file, get file contents.
        Otherwise, get stdout and stderr.
        For each file, only return the first few lines.
        """
        def get_lines(name):
            lines = self.head_target((uuid, name), self.HEAD_MAX_LINES)
            if lines is not None:
                lines = ''.join(map(base64.b64decode, lines))

            return formatting.verbose_contents_str(lines)

        info = self.get_target_info((uuid, ''), 2)  # List files
        if info['type'] == 'file':
            info['file_contents'] = get_lines('')
        else:
            # Read contents of stdout and stderr.
            info['stdout'] = None
            info['stderr'] = None
            contents = info.get('contents')
            if contents:
                for item in contents:
                    name = item['name']
                    if name in ['stdout', 'stderr']:
                        info[name] = get_lines(name)
        return info

    def get_target_info(self, target, depth=1):
        info = self.client.get_target_info(target, depth)
        contents = info.get('contents')
        # Render the sizes
        if contents:
            for item in contents:
                if 'size' in item:
                    item['size_str'] = formatting.size_str(item['size'])
        return info

    # Create an instance of a CLI.
    def _create_cli(self, worksheet_uuid):
        output_buffer = StringIO()
        manager = CodaLabManager(temporary=True, clients={'local': self.client})
        manager.set_current_worksheet_uuid(self.client, worksheet_uuid)
        cli = bundle_cli.BundleCLI(manager, headless=True, stdout=output_buffer, stderr=output_buffer)
        return cli, output_buffer

    def complete_command(self, worksheet_uuid, command):
        """
        Given a command string, return a list of suggestions to complete the last token.
        """
        cli, output_buffer = self._create_cli(worksheet_uuid)

        command = command.lstrip()
        if not command.startswith('cl'):
            command = 'cl ' + command

        return cli.complete_command(command)

    def get_command(self, raw_command_map):
        """
        Return a cli-command corresponding to raw_command_map contents.
        Input:
            raw_command_map: a map containing the info to edit, new_value and the action to perform
        """
        return worksheet_util.get_worksheet_info_edit_command(raw_command_map)

    def general_command(self, worksheet_uuid, command):
        """
        Executes an arbitrary CLI command with |worksheet_uuid| as the current worksheet.
        Basically, all CLI functionality should go through this command.
        The method currently intercepts stdout/stderr and returns it back to the user.
        """
        # Tokenize
        if isinstance(command, basestring):
            args = shlex.split(command)
        else:
            args = list(command)

        # Ensure command always starts with 'cl'
        if args[0] == 'cl':
            args = args[1:]

        cli, output_buffer = self._create_cli(worksheet_uuid)
        exception = None
        structured_result = None
        try:
            structured_result = cli.do_command(args)
        except SystemExit:  # as exitcode:
            # this should not happen under normal circumstances
            pass
        except BaseException as e:
            exception = str(e)

        output_str = output_buffer.getvalue()
        output_buffer.close()

        return {
            'structured_result': structured_result,
            'output': output_str,
            'exception': exception
        }

    def update_bundle_metadata(self, uuid, new_metadata):
        self.client.update_bundle_metadata(uuid, new_metadata)
        return


class RemoteBundleService(object):
    '''
    Adapts the RemoteBundleClient for REST calls.

    TODO(klopyrev): This version should eventually go away once the file upload
    logic is cleaned up. See below where this class is used for more information.
    '''
    def __init__(self):
        self.client = RemoteBundleClient(self._cli_url(),
                                         lambda command: self._get_user_token(), verbose=0)

    def _cli_url(self):
        return 'http://' + local.config['server']['host'] + ':' + str(local.config['server']['port'])

    def _get_user_token(self):
        """
        Returns an access token for the user. This function facilitates interactions
        with the bundle service.
        """
        CLIENT_ID = 'codalab_cli_client'
    
        if request.user is None:
            return None
    
        # Try to find an existing token that will work.
        token = local.model.find_oauth2_token(
            CLIENT_ID,
            request.user.user_id,
            datetime.utcnow() + timedelta(minutes=5))
        if token is not None:
            return token.access_token
    
        # Otherwise, generate a new one.
        token = OAuth2Token(
            local.model,
            access_token=generate_token(),
            refresh_token=None,
            scopes='',
            expires=datetime.utcnow() + timedelta(hours=10),
            client_id=CLIENT_ID,
            user_id=request.user.user_id,
        )
        local.model.save_oauth2_token(token)
    
        return token.access_token

    def upload_bundle(self, source_file, bundle_type, worksheet_uuid):
        """
        Upload |source_file| (a stream) to |worksheet_uuid|.
        """
        # Construct info for creating the bundle.
        bundle_subclass = get_bundle_subclass(bundle_type) # program or data
        metadata = metadata_util.fill_missing_metadata(bundle_subclass, {}, initial_metadata={'name': source_file.filename, 'description': 'Upload ' + source_file.filename})
        info = {'bundle_type': bundle_type, 'metadata': metadata}

        # Upload it by creating a file handle and copying source_file to it (see RemoteBundleClient.upload_bundle in the CLI).
        remote_file_uuid = self.client.open_temp_file(metadata['name'])
        dest = RPCFileHandle(remote_file_uuid, self.client.proxy)
        file_util.copy(source_file.file, dest, autoflush=False, print_status='Uploading %s' % metadata['name'])
        dest.close()

        pack = False  # For now, always unpack (note: do this after set remote_file_uuid, which needs the extension)
        if not pack and zip_util.path_is_archive(metadata['name']):
            metadata['name'] = zip_util.strip_archive_ext(metadata['name'])

        # Then tell the client that the uploaded file handle is there.
        new_bundle_uuid = self.client.finish_upload_bundle(
            [remote_file_uuid],
            not pack,  # unpack
            info,
            worksheet_uuid,
            True)  # add_to_worksheet
        return new_bundle_uuid


logger = logging.getLogger(__name__)


def log_exception(exception, traceback):
    logging.error(request.route.method + ' ' + request.route.rule)
    logging.error(str(exception))
    logging.error('')
    logging.error('-------------------------')
    logging.error(traceback)
    logging.error('-------------------------')


@get('/worksheets/sample/')
def get_sample_worksheets():
    '''
    Get worksheets to display on the front page.
    Keep only |worksheet_uuids|.
    '''
    service = BundleService()

    # Select good high-quality worksheets and randomly choose some
    list_worksheets = service.search_worksheets(['tag=paper,software,data'])
    list_worksheets = random.sample(list_worksheets, min(3, len(list_worksheets)))

    # Always put home worksheet in
    list_worksheets = service.search_worksheets(['name=home']) + list_worksheets

    # Reformat
    list_worksheets = [{'uuid': val['uuid'],
                        'display_name': val.get('title') or val['name'],
                        'owner_name': val['owner_name']} for val in list_worksheets]

    response.content_type = 'application/json'
    return json.dumps(list_worksheets)


@get('/worksheets/')
def get_worksheets_landing():
    requested_ws = request.query.get('uuid', request.query.get('name', 'home'))
    service = BundleService()
    try:
        uuid = service.get_worksheet_uuid(requested_ws)
    except Exception as e:  # UsageError
        return HTTPError(status=httplib.NOT_FOUND, body=e.message)
    redirect('/worksheets/%s/' % uuid)


@post('/api/worksheets/command/')
def post_worksheets_command():
    # TODO(klopyrev): The Content-Type header is not set correctly in
    # editable_field.jsx, so we can't use request.json.
    data = json.loads(request.body.read())
    service = BundleService()

    if data.get('raw_command', None):
        data['command'] = service.get_command(data['raw_command'])

    if not data.get('worksheet_uuid', None) or not data.get('command', None):
        return HTTPResponse("Must have worksheet uuid and command", status=httplib.BAD_REQUEST)

    # If 'autocomplete' field is set, return a list of completions instead
    if data.get('autocomplete', False):
        return {
            'completions': service.complete_command(data['worksheet_uuid'], data['command'])
        }

    result = service.general_command(data['worksheet_uuid'], data['command'])
    # The return value is a list, so the normal Bottle JSON return-value logic
    # doesn't apply since it handles only dicts.
    response.content_type = 'application/json'
    return json.dumps(result)


@get('/api/worksheets/<uuid:re:%s>/' % spec_util.UUID_STR)
def get_worksheet_content(uuid):
    service = BundleService()
    try:
        return service.full_worksheet(uuid)
    except Exception as e:
        log_exception(e, traceback.format_exc())
        return HTTPResponse({"error": str(e)}, status=httplib.INTERNAL_SERVER_ERROR)


@post('/api/worksheets/<uuid:re:%s>/' % spec_util.UUID_STR,
      apply=AuthenticatedPlugin())
def post_worksheet_content(uuid):
    data = request.json

    worksheet_uuid = data['uuid']
    lines = data['lines']

    if worksheet_uuid != uuid:
        return HTTPResponse(None, status=httplib.FORBIDDEN)

    service = BundleService()
    try:
        service.parse_and_update_worksheet(worksheet_uuid, lines)
        return {}
    except Exception as e:
        log_exception(e, traceback.format_exc())
        return HTTPResponse({"error": str(e)}, status=httplib.INTERNAL_SERVER_ERROR)


@get('/api/bundles/content/<uuid:re:%s>/' % spec_util.UUID_STR)
@get('/api/bundles/content/<uuid:re:%s>/<path:path>/' % spec_util.UUID_STR)
def get_bundle_content(uuid, path=''):
    service = BundleService()
    try:
        target = (uuid, path)
        return service.get_target_info(target)
    except Exception as e:
        log_exception(e, traceback.format_exc())
        return HTTPResponse({"error": str(e)}, status=httplib.INTERNAL_SERVER_ERROR)


@post('/api/bundles/upload/')
def post_bundle_upload():
    # TODO(klopyrev): This file upload logic is not optimal. The upload goes
    # to the remote XML RPC bundle service, just like it did before when this
    # API was implemented in Django. Ideally, this REST server should just store
    # the upload to the bundle store directly. A bunch of logic needs to be
    # cleaned up in order for that to happen.
    service = RemoteBundleService()
    try:
        source_file = request.files['file']
        bundle_type = request.POST['bundle_type']
        worksheet_uuid = request.POST['worksheet_uuid']
        new_bundle_uuid =  service.upload_bundle(source_file, bundle_type, worksheet_uuid)
        return {'uuid': new_bundle_uuid}
    except Exception as e:
        log_exception(e, traceback.format_exc())
        return HTTPResponse({"error": str(e)}, status=httplib.INTERNAL_SERVER_ERROR)


@get('/api/bundles/<uuid:re:%s>/' % spec_util.UUID_STR)
def get_bundle_info(uuid):
    service = BundleService()
    try:
        bundle_info = service.get_bundle_info(uuid)
        if bundle_info is None:
            return HTTPResponse({'error': 'The bundle is not available'})
        bundle_info.update(service.get_bundle_contents(uuid))
        return bundle_info
    except Exception as e:
        log_exception(e, traceback.format_exc())
        return HTTPResponse({"error": str(e)}, status=httplib.INTERNAL_SERVER_ERROR)


@post('/api/bundles/<uuid:re:%s>/' % spec_util.UUID_STR)
def post_bundle_info(uuid):
    '''
    Save metadata information for a bundle.
    '''
    service = BundleService()
    try:
        bundle_info = service.get_bundle_info(uuid)
        # Save only if we're the owner.
        if bundle_info['edit_permission']:
            # TODO(klopyrev): The Content-Type header is not set correctly in
            # editable_field.jsx, so we can't use request.json.
            data = json.loads(request.body.read())
            new_metadata = data['metadata']

            # TODO: do this generally based on the CLI specs.
            # Remove generated fields.
            for key in ['data_size', 'created', 'time', 'time_user', 'time_system', 'memory', 'disk_read', 'disk_write', 'exitcode', 'actions', 'started', 'last_updated']:
                if key in new_metadata:
                    del new_metadata[key]

            # Convert to arrays
            for key in ['tags', 'language', 'architectures']:
                if key in new_metadata and isinstance(new_metadata[key], basestring):
                    new_metadata[key] = new_metadata[key].split(',')

            # Convert to ints
            for key in ['request_cpus', 'request_gpus', 'request_priority']:
                if key in new_metadata:
                    new_metadata[key] = int(new_metadata[key])

            service.update_bundle_metadata(uuid, new_metadata)
            bundle_info = service.get_bundle_info(uuid)
            return bundle_info
        else:
            return {'error': 'Can\'t save unless you\'re the owner'}
    except Exception as e:
        log_exception(e, traceback.format_exc())
        # TODO(klopyrev): Not sure why this API doesn't return the status code
        # 500 as do the others.
        return {"error": str(e)}