"""Generate cloud masks from satellite images."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from typing import Dict, Generator, List, Optional, Text, Tuple

import datetime
import io
import logging  # pylint: disable=unused-import
import os

from absl import app
from absl import flags

import apache_beam as beam
import dateparser
import dateutil
import netCDF4  # pylint: disable=unused-import
import numpy as np  # pylint: disable=unused-import
from PIL import Image
import tensorflow as tf  # pylint: disable=unused-import

from google.cloud import storage as gcs

from goes_truecolor.lib import goes_reader  # pylint: disable=unused-import
from goes_truecolor.learning import hparams  # pylint: disable=unused-import


flags.DEFINE_string(
    'project', 'weather-324',
    'Name of the project for billing')

flags.DEFINE_string(
    'goes_bucket', 'gcp-public-data-goes-16',
    'GOES bucket')

flags.DEFINE_string(
    'tmp_dir', 'gs://weather-tmp',
    'Temporary files bucket')

flags.DEFINE_string(
    'model_dir', 'gs://weather-datasets/model/export/exporter',
    'Directory containing the model')

flags.DEFINE_string(
    'output_bucket', 'weather-datasets',
    'Bucket containing the output')

flags.DEFINE_string(
    'output_dir', 'cloud_masks',
    'Directory containing the output cloud masks')

flags.DEFINE_string(
    'output_summary', 'gs://weather-datasets/cloud_masks_summary',
    'Output summary file')

flags.DEFINE_string(
    'runner', 'DirectRunner',
    'Which beam runner to use; '
    'use DataflowRunner to run on cloud')

flags.DEFINE_integer(
    'max_workers', 10,
    'Maximum number of worker processes')

flags.DEFINE_integer(
    'image_size', 1024, 'size of the images (images are square)')

flags.DEFINE_integer(
    'tile_size', 64, 'size of tiles')

flags.DEFINE_integer(
    'tile_border_size', 8, 'tile border size to be cropped')

flags.DEFINE_string(
    'start_date', '1/1/2018 17:00',
    'Start date for collecting images')

flags.DEFINE_string(
    'end_date', '12/31/2018 17:00',
    'End date for collecting images (inclusive)')


FLAGS = flags.FLAGS


# pylint: disable=abstract-method,too-many-instance-attributes
class CreateCloudMasks(beam.DoFn):
  """Create cloud mask images from GOES images."""

  def __init__(  # pylint: disable=too-many-arguments
      self,
      project_id: Text,
      goes_bucket_name: Text,
      image_size: int,
      tile_size: int,
      tile_border_size: int,
      model_dir: Text,
      output_bucket: Text,
      output_dir: Text,
      ir_channels: List[int],
      tmp_dir: Optional[Text] = None,
      gcs_client: Optional[gcs.Client] = None):
    # pylint: disable=super-init-not-called
    """Read a GOES snapshot and construct a cloud mask.

    Args:
      project_id: the billing account
      goes_bucket_name: the name of the bucket containg the GOES images
      image_size: the total image size (square)
      tile_size: the size of each example (square)
      ir_channels: the IR channels.
      model_dir: the directory containing the model.
      output_bucket: the output bucket.
      output_dir: the output directory.
      tmp_dir: a temporary directory.
      gcs_client: the GCS client object (for testing)
    """
    self.project_id = project_id
    self.goes_bucket_name = goes_bucket_name
    self.image_size = image_size
    self.tile_size = tile_size
    self.tile_border_size = tile_border_size
    self.model_dir = model_dir
    self.output_bucket = output_bucket
    self.output_dir = output_dir
    self.ir_channels = ir_channels
    self.reader = None
    self.tmp_dir = tmp_dir
    self.gcs_client = gcs_client
    self.model = None

  # pylint: disable=arguments-differ,too-many-locals
  def process(
      self, files: Tuple[datetime.datetime, Dict[int, Text]]) -> Generator[Text, None, None]:
    # pylint: disable=reimported,redefined-outer-name
    import io
    import logging
    import numpy as np
    import os
    from PIL import Image
    from google.api_core import client_info
    from google.cloud import storage as gcs
    from goes_truecolor.lib import goes_predict
    from goes_truecolor.lib import goes_reader

    _, file_table = files

    # Create the GoesReader lazily so that beam will not pickle it
    # when copying this object to other workers.
    if self.reader is None:
      logging.info('creating GoesReader')
      shape = self.image_size, self.image_size
      self.reader = goes_reader.GoesReader(
          project_id=self.project_id,
          goes_bucket_name=self.goes_bucket_name, shape=shape,
          tmp_dir=self.tmp_dir, client=self.gcs_client, cache=False)

    # Fetch the model.
    if self.model is None:
      self.model = goes_predict.GoesPredict(
          self.model_dir, self.tile_size, self.tile_border_size)

    # Datastore client.
    if self.gcs_client is None:
      self.gcs_client = gcs.Client(project=self.project_id)

    # Fetch the images and perform the prediction.
    logging.info('creating IR image')
    ir = self.reader.load_channel_images_from_files(file_table, self.ir_channels)
    ir_img, md = goes_reader.flatten_channel_images(ir, self.ir_channels)
    cloud_img = self.model.predict(ir_img)

    # Get jpeg bytes.
    cloud_img = Image.fromarray(cloud_img)
    buf = io.BytesIO()
    cloud_img.save(buf, format='JPEG', quality=70)
    buf = buf.getvalue()

    # Write to a file.
    t = md['time_coverage_start']
    dirname = t.strftime('%Y/%j')
    filename = os.path.join(self.output_dir, t.strftime('%Y/%j'), t.isoformat() + '.jpg')
    bucket = self.gcs_client.get_bucket(self.output_bucket)
    blob = bucket.blob(filename)
    blob.upload_from_string(buf)
    yield filename


def main(unused_argv):
  """Beam pipeline to create examples."""
  # Get the files to process.
  utc = dateutil.tz.tzutc()
  start_date = dateparser.parse(FLAGS.start_date)
  start_date = start_date.replace(tzinfo=utc)
  end_date = dateparser.parse(FLAGS.end_date)
  end_date = end_date.replace(tzinfo=utc)
  reader = goes_reader.GoesReader(
      project_id=FLAGS.project,
      goes_bucket_name=FLAGS.goes_bucket,
      shape=(FLAGS.image_size, FLAGS.image_size))
  files = reader.list_time_range(start_date, end_date)

  # Create the beam pipeline.
  options = {'staging_location': os.path.join(FLAGS.tmp_dir, 'tmp', 'staging'),
             'temp_location': os.path.join(FLAGS.tmp_dir, 'tmp'),
             'job_name': datetime.datetime.now().strftime('cloud-masks-%y%m%d-%H%M%S'),
             'project': FLAGS.project,
             'num_workers': 1,
             'max_num_workers': FLAGS.max_workers,
             'machine_type': 'n1-standard-4',
             'setup_file': os.path.join(
                 os.path.dirname(os.path.abspath(__file__)), '../../setup.py'),
             'teardown_policy': 'TEARDOWN_ALWAYS',
             'save_main_session': False}
  opts = beam.pipeline.PipelineOptions(flags=[], **options)

  # Run the beam pipeline.
  # with beam.Pipeline(FLAGS.runner, options=opts) as p:
  #   (p  # pylint: disable=expression-not-assigned
  #    | beam.io.ReadFromPubSub( 'projects/gcp-public-data---goes-16/topics/gcp-public-data-goes-16')
  #    | beam.FlatMap(lambda s: print(s)))

  # Run the beam pipeline.
  with beam.Pipeline(FLAGS.runner, options=opts) as p:
    (p  # pylint: disable=expression-not-assigned
     | beam.Create(files)
     | beam.ParDo(CreateCloudMasks(
         project_id=FLAGS.project,
         goes_bucket_name=FLAGS.goes_bucket,
         image_size=FLAGS.image_size,
         tile_size=FLAGS.tile_size,
         tile_border_size=FLAGS.tile_border_size,
         ir_channels=goes_reader.IR_CHANNELS,
         model_dir=FLAGS.model_dir,
         output_bucket=FLAGS.output_bucket,
         output_dir=FLAGS.output_dir))
     | beam.io.WriteToText(FLAGS.output_summary))


if __name__ == '__main__':
  app.run(main)
