import imp
import json
import os
import datetime
import uuid
from string import Template
from concurrent.futures import ThreadPoolExecutor
import subprocess
import shutil

import requests
from flask import Blueprint, request, send_file, send_from_directory

from common.util import make_failure, make_success, make_response
from common.util import to_report_dict
from common.util import TOKENS
from config import WORKDIR, INTERNALIP
from db.models import Report
from db.interface import db

report_route = Blueprint('report', __name__, url_prefix='/report', template_folder='templates', static_folder='static')

executor = ThreadPoolExecutor()

TPL = '''Arguments for Gas phase
geom_file_name=$mfile
orca_param=
orca_executable=/home/azureuser/orca/orca
experimental_spectra=$efile
experimental_energy_column_number=$ce
experimental_intensity_column_number=$ci
experimental_number_columns=$cn
experimental_header_skip=$offset
element_calculate=N
'''


def run_cmd(cmd):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    stdout, stderr = proc.communicate()
    result_code = proc.poll()
    if result_code != 0:
        output = 'stdout: "%s", stderr: "%s"' % (stdout, stderr)
        raise subprocess.CalledProcessError(result_code, cmd, output)
    return stdout.rstrip()


def valid_login(user_name, token):
    _token = TOKENS.get(user_name, '')
    return _token == token


# Asynchronously process the report, the process and results are updated to the database synchronously
@report_route.route('/process', methods=['POST'])
def process():
    try:
        body_args = request.values
        if request.data:
            body_args = json.loads(request.data)

        report_id = body_args.get("report_id")
        # start building
        updates = {"status": "building"}
        Report.query.filter_by(id=report_id).update(updates)
        db.session.commit()

        # start processing
        target_dir = os.path.join(WORKDIR, report_id)
        current_dir = os.getcwd()

        os.chdir(target_dir)  # equal to command: cd /home/azureuser/flask/21220835
        cmd = ['python', '/home/azureuser/XAS_analysis/GW1/GW1.py', 'G_args.txt']
        run_cmd(' '.join(cmd))

        _, c1_file = get_report_file(target_dir)
        # finish
        updates = {'status': 'finished', 'name': c1_file}
        Report.query.filter_by(id=report_id).update(updates)
        db.session.commit()

        os.chdir(current_dir)
        return make_success('')
    except Exception as e:
        updates = {'status': 'failed'}
        Report.query.filter_by(id=report_id).update(updates)
        db.session.commit()

        return make_failure(e)


def _get_report_file(target_dir):
    for root,dirs,files in os.walk(target_dir):
        if 'C1_N1s_Imidazole_ISEELS' in root:
            return root


def get_report_file(target_dir):
    c1 = _get_report_file(target_dir)
    if not c1:
        raise Exception('failed to find C1_N1s dir')

    parts = c1.split('/')
    return c1, parts[-1][3:]


# Internal request call to temporarily initialize database
def do_process(report_id):
    formdata = {
        "report_id": report_id
    }
    requests.post("http://{internal_ip}:8888/report/process".format(internal_ip=INTERNALIP), data=formdata)


# Build report records based on uploaded files
@report_route.route('/upload', methods=['POST'])
def upload_report():
    try:
        req_args = request.args
        # user name
        user_name = req_args.get('user_name')
        if not user_name:
            return make_failure("invaild empty user name")
        if not valid_login(user_name, request.headers.get('Authorization')):
            return make_response(401, '', 'user not login')

        # Each user can have up to 10 reports at the same time
        reports = Report.query.filter_by(owner=user_name).all()
        if len(reports) >= 10:
            return make_failure('Sorry, only ten reports are allowed to be saved. Please delete some reports')

        process_id = str(uuid.uuid1())
        db.session.add(Report(
            status="waiting",
            progress=process_id,
            create_at=str(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
            owner=user_name))
        db.session.commit()

        newReport = Report.query.filter_by(progress=process_id).first()
        target_dir = os.path.join(WORKDIR, str(newReport.id))
        # create target folder
        os.mkdir(target_dir)

        # save file and parameters
        efile = request.files.get('efile')
        efile_target = os.path.join(target_dir, efile.filename)
        efile.save(efile_target)

        mfile = request.files.get('mfiles')
        mfile_target = os.path.join(target_dir, mfile.filename)
        mfile.save(mfile_target)

        # The front end transmits two files and 4 business parameters
        # parameters are not verified here, instead,they have been verified at the front end
        column_energy = req_args.get('ce')
        column_number = req_args.get('cn')
        column_intensity = req_args.get('ci')
        offset = req_args.get('offset')

        args_target = os.path.join(target_dir, 'G_args.txt')
        with open(args_target, 'w') as f:
            f.write(Template(TPL).substitute(
                ce=column_energy,
                ci=column_intensity,
                cn=column_number,
                offset=offset,
                efile=efile_target,
                mfile=mfile_target))

        # Return directly, and start processing asynchronously
        executor.submit(do_process, str(newReport.id))
        return make_success('')
    except Exception as e:
        return make_failure(e)


# Make sure the folder for the image exists and is empty
def clean_image_dir(image_dir):
    if os.path.exists(image_dir):
        shutil.rmtree(image_dir) # delete the old image dir

    os.mkdir(image_dir) # renew


def prepare_display_image(c1_dir, image_dir):
    clean_image_dir(image_dir)
    # prepare images
    for png in ['Norm_Exp-Theory.png',
                'Norm_Trans_Exp-Theory.png',
                'Norm_Trans_Exp-Theory_PeakAssign.png',
                'Raw_Exp-Theory.png']:
        cmd = ['cp', os.path.join(c1_dir, png), image_dir]
        run_cmd(' '.join(cmd))


@report_route.route('/html', methods=['GET'])
def preview_online():
    try:
        report_id = request.args.get("report_id")
        if not report_id:
            return make_failure('invaild empty report id')

        image_dir = os.path.join(WORKDIR, 'images')
        target_dir = os.path.join(WORKDIR, report_id)

        c1_dir, _ = get_report_file(target_dir)

        prepare_display_image(c1_dir, image_dir)
        return send_from_directory(c1_dir, "N1s_Imidazole_ISEELS_C1_report.html")
    except Exception as e:
        return make_failure(e)


@report_route.route('/Norm_Exp-Theory.png', methods=['GET'])
def download1():
    try:
        return send_file(os.path.join(WORKDIR, 'images', 'Norm_Exp-Theory.png'))
    except Exception as e:
        return make_failure(e)


@report_route.route('/Norm_Trans_Exp-Theory.png', methods=['GET'])
def download2():
    try:
        return send_file(os.path.join(WORKDIR, 'images', 'Norm_Trans_Exp-Theory.png'))
    except Exception as e:
        return make_failure(e)


@report_route.route('/Norm_Trans_Exp-Theory_PeakAssign.png', methods=['GET'])
def download3():
    try:
        return send_file(os.path.join(WORKDIR, 'images', 'Norm_Trans_Exp-Theory_PeakAssign.png'))
    except Exception as e:
        return make_failure(e)


@report_route.route('/Raw_Exp-Theory.png', methods=['GET'])
def download4():
    try:
        return send_file(os.path.join(WORKDIR, 'images', 'Raw_Exp-Theory.png'))
    except Exception as e:
        return make_failure(e)


@report_route.route('/download', methods=['GET'])
def download_report():
    try:
        report_id = request.args.get("report_id")
        if not report_id:
              return make_failure('invaild empty report id')

        target_dir = os.path.join(WORKDIR, report_id)
        current_dir = os.getcwd()

        os.chdir(target_dir)

        # download after compressing
        c1_dir, c1_file = get_report_file(target_dir)

        cmd = ['rm', '-rf', c1_file+'.tar']
        run_cmd(' '.join(cmd))

        cmd = ['tar', '-cvf', c1_file+'.tar',  c1_dir]
        run_cmd(' '.join(cmd))

        os.chdir(current_dir)

        return send_file(target_dir+'/'+ c1_file+'.tar', as_attachment=True)
    except Exception as e:
        return make_failure(e)


@report_route.route('/delete', methods=['DELETE'])
def delete_report():
    import pdb;pdb.set_trace()
    user_name = request.args.get('user_name')
    if not valid_login(user_name, request.headers.get('Authorization')):
        return make_response(401, '', 'user not login')

    report_id = request.args.get("report_id")
    if not report_id:
        return make_failure('invaild empty report id')
    try:
        report = Report.query.filter_by(id=report_id).first()
        db.session.delete(report)
        db.session.commit()

        target_dir = os.path.join(WORKDIR, report_id)
        shutil.rmtree(target_dir)

        return make_success('')
    except Exception as e:
        return make_failure(e)


@report_route.route('/list', methods=['GET'])
def get_reports():
    try:
        user_name = request.args.get('user_name')
        if not valid_login(user_name, request.headers.get('Authorization')):
            return make_response(401, '', 'user not login')

        reports = Report.query.filter_by(owner=user_name).all()
        report_list = []
        for report in reports:
            report_list.append(to_report_dict(report))

        return make_success(report_list)
    except Exception as e:
        return make_failure(e)
