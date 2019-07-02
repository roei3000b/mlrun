# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
#import pandas as pd
from .datastore import StoreManager
from .rundb import RunDBInterface
from .utils import uxjoin, run_keys, ModelObj


class ArtifactManager:

    def __init__(self, stores: StoreManager,execution=None,
                 db: RunDBInterface = None,
                 out_path='',
                 calc_hash=True):
        self._execution = execution
        self.out_path = out_path
        self.calc_hash = calc_hash

        self.data_stores = stores
        self.artifact_db = db
        self.input_artifacts = {}
        self.output_artifacts = {}
        self.outputs_spec = {}

    def from_dict(self, struct: dict):
        self.out_path = struct.get(run_keys.output_path, self.out_path)
        out_list = struct.get(run_keys.output_artifacts)
        if out_list and isinstance(out_list, list):
            for item in out_list:
                self.outputs_spec[item['key']] = item.get('path')

    def to_dict(self, struct):
        struct['spec'][run_keys.output_artifacts] = [{'key':k, 'path':v} for k, v in self.outputs_spec.items()]
        struct['spec'][run_keys.output_path] = self.out_path
        struct['status'][run_keys.output_artifacts] = [item.base_dict() for item in self.output_artifacts.values()]

    def log_artifact(self, item, body=None, target_path='', src_path='',
                     tag='', viewer='', upload=True):
        if isinstance(item, str):
            key = item
            item = Artifact(key, body, src_path=src_path,
                            tag=tag, viewer=viewer)
        else:
            key = item.key
            target_path = target_path or item.target_path
            item.src_path = src_path or item.src_path
            item.viewer = viewer or item.viewer

        # find the target path from defaults and config
        if key in self.outputs_spec.keys():
            target_path = self.outputs_spec[key] or target_path
        if not target_path:
            target_path = uxjoin(self.out_path, key)
        item.target_path = target_path
        item.tag = tag or item.tag or self._execution.tag

        self.output_artifacts[key] = item

        if upload:
            store, ipath = self.get_store(target_path)
            body = body or item.get_body()
            if body:
                store.put(ipath, body)
            else:
                src_path = src_path or key
                if os.path.isfile(src_path):
                    store.upload(ipath, src_path)

        if self.artifact_db:
            if not item.sources:
                item.sources = self._execution.to_dict()['spec'][run_keys.input_objects]
            item.execution = self._execution.get_meta()
            self.artifact_db.store_artifact(key, item, item.tag, self._execution.project)

    def get_store(self, url):
        return self.data_stores.get_or_create_store(url)


class Artifact(ModelObj):

    _dict_fields = ['key', 'src_path', 'target_path', 'hash', 'description', 'viewer']
    kind = ''

    def __init__(self, key, body=None, src_path='', target_path='', tag='', viewer=''):
        self._key = key
        self.tag = tag
        self.target_path = target_path
        self.src_path = src_path
        self._body = body
        self.description = ''
        self.viewer = viewer
        self.encoding = ''
        self.sources = []
        self.execution = None
        self.hash = None
        self.license = ''

    @property
    def key(self):
        return self._key

    def get_body(self):
        return self._body

    def base_dict(self):
        return super().to_dict()

    def to_dict(self):
        return super().to_dict(self._dict_fields + ['execution', 'sources'])


chart_template = '''
<html>
  <head>
    <script type="text/javascript" src="https://www.gstatic.com/charts/loader.js"></script>
    <script type="text/javascript">
      google.charts.load('current', {'packages':['corechart']});
      google.charts.setOnLoadCallback(drawChart);
      function drawChart() {
        var data = google.visualization.arrayToDataTable($data$);
        var options = $opts$;
        var chart = new google.visualization.$chart$(document.getElementById('chart_div'));
        chart.draw(data, options);
      }
    </script>
  </head>
  <body>
    <div id="chart_div" style="width: 100%; height: 500px;"></div>
  </body>
</html>
'''

class ChartArtifact(Artifact):
    kind = 'chart'

    def __init__(self, key, data=[], src_path='', target_path='', tag='',
                         viewer='chart', options={}):
        super().__init__(key, None, src_path, target_path, tag, viewer)
        self.header = []
        self._rows = []
        if data:
            self.header = data[0]
            self._rows = data[1:]
        self.options = options
        self.chart = 'LineChart'

    def add_row(self, row):
        self._rows += [row]

    def get_body(self):
        if not self.options.get('title'):
            self.options['title'] = self.key
        data = [self.header] + self._rows
        return chart_template.replace('$data$', json.dumps(data))\
            .replace('$opts$', json.dumps(self.options))\
            .replace('$chart$', self.chart)


class TableArtifact(Artifact):
    _dict_fields = ['key', 'src_path', 'target_path', 'hash', 'description',
                    'format', 'schema', 'header', 'viewer']
    kind = 'table'

    def __init__(self, key, body=None, src_path='', target_path='', tag='',
                         viewer='', format='', header=[], schema=None):
        super().__init__(key, body, src_path, target_path, tag, viewer)
        self.format = format
        self.schema = schema
        self.header = header

    def from_df(self, df, format=''):
        format = format or self.format
        # todo: read pandas into body/file


def write_df(df, format, path):
    pass