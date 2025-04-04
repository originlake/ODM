import os
import shutil
import struct
import pipes
import fiona
import fiona.crs
import json
import zipfile
import math
from collections import OrderedDict
from pyproj import CRS
import pdal

from opendm import io
from opendm import log
from opendm import types
from opendm import system
from opendm import context
from opendm import location
from opendm.cropper import Cropper
from opendm import point_cloud
from opendm.multispectral import get_primary_band_name
from opendm.osfm import OSFMContext
from opendm.boundary import as_polygon, export_to_bounds_files
from opendm.align import compute_alignment_matrix, transform_point_cloud, transform_obj
from opendm.utils import np_to_json
from opendm.georef import TopocentricToProj

class ODMGeoreferencingStage(types.ODM_Stage):
    def process(self, args, outputs):
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']

        # Export GCP information if available

        gcp_export_file = tree.path("odm_georeferencing", "ground_control_points.gpkg")
        gcp_gml_export_file = tree.path("odm_georeferencing", "ground_control_points.gml")
        gcp_geojson_export_file = tree.path("odm_georeferencing", "ground_control_points.geojson")
        gcp_geojson_zip_export_file = tree.path("odm_georeferencing", "ground_control_points.zip")
        unaligned_model = io.related_file_path(tree.odm_georeferencing_model_laz, postfix="_unaligned")
        if os.path.isfile(unaligned_model) and self.rerun():
            os.unlink(unaligned_model)

        if reconstruction.has_gcp() and (not io.file_exists(gcp_export_file) or self.rerun()):
            octx = OSFMContext(tree.opensfm)
            gcps = octx.ground_control_points(reconstruction.georef.proj4())

            if len(gcps):
                gcp_schema = {
                    'geometry': 'Point',
                    'properties': OrderedDict([
                        ('id', 'str'),
                        ('observations_count', 'int'),
                        ('observations_list', 'str'),
                        ('error_x', 'float'),
                        ('error_y', 'float'),
                        ('error_z', 'float'),
                    ])
                }

                # Write GeoPackage
                with fiona.open(gcp_export_file, 'w', driver="GPKG",
                                crs=fiona.crs.from_string(reconstruction.georef.proj4()),
                                schema=gcp_schema) as f:
                    for gcp in gcps:
                        f.write({
                            'geometry': {
                                'type': 'Point',
                                'coordinates': gcp['coordinates'],
                            },
                            'properties': OrderedDict([
                                ('id', gcp['id']),
                                ('observations_count', len(gcp['observations'])),
                                ('observations_list', ",".join([obs['shot_id'] for obs in gcp['observations']])),
                                ('error_x', gcp['error'][0]),
                                ('error_y', gcp['error'][1]),
                                ('error_z', gcp['error'][2]),
                            ])
                        })

                # Write GML
                try:
                    system.run('ogr2ogr -of GML "{}" "{}"'.format(gcp_gml_export_file, gcp_export_file))
                except Exception as e:
                    log.ODM_WARNING("Cannot generate ground control points GML file: %s" % str(e))

                # Write GeoJSON
                geojson = {
                    'type': 'FeatureCollection',
                    'features': []
                }

                from_srs = CRS.from_proj4(reconstruction.georef.proj4())
                to_srs = CRS.from_epsg(4326)
                transformer = location.transformer(from_srs, to_srs)

                for gcp in gcps:
                    properties = gcp.copy()
                    del properties['coordinates']

                    geojson['features'].append({
                        'type': 'Feature',
                        'geometry': {
                            'type': 'Point',
                            'coordinates': transformer.TransformPoint(*gcp['coordinates']),
                        },
                        'properties': properties
                    })

                with open(gcp_geojson_export_file, 'w') as f:
                    f.write(json.dumps(geojson, indent=4))
                
                with zipfile.ZipFile(gcp_geojson_zip_export_file, 'w', compression=zipfile.ZIP_LZMA) as f:
                    f.write(gcp_geojson_export_file, arcname=os.path.basename(gcp_geojson_export_file))

            else:
                log.ODM_WARNING("GCPs could not be loaded for writing to %s" % gcp_export_file)
                
        if reconstruction.is_georeferenced():
            # prepare pipeline stage for topocentric to georeferenced conversion
            octx = OSFMContext(tree.opensfm)
            reference = octx.reference()
            converter = TopocentricToProj(reference.lat, reference.lon, reference.alt, reconstruction.georef.proj4())
        
        if not io.file_exists(tree.filtered_point_cloud) or self.rerun():
            log.ODM_INFO("Georeferecing filtered point cloud")
            if reconstruction.is_georeferenced():
                pipeline = pdal.Reader.ply(tree.filtered_point_cloud_topo).pipeline()
                pipeline.execute()
                arr = pipeline.arrays[0]
                arr = converter.convert_array(
                    arr,
                    reconstruction.georef.utm_east_offset,
                    reconstruction.georef.utm_north_offset
                )
                pipeline = pdal.Writer.ply(
                    filename = tree.filtered_point_cloud,
                    storage_mode = "little endian",
                ).pipeline(arr)
                pipeline.execute()
            else:
                shutil.copy(tree.filtered_point_cloud_topo, tree.filtered_point_cloud)

        def georefernce_textured_model(obj_in, obj_out):
            log.ODM_INFO("Georeferecing textured model %s" % obj_in)
            if not io.file_exists(obj_out) or self.rerun():
                if reconstruction.is_georeferenced():
                    converter.convert_obj(
                        obj_in, 
                        obj_out, 
                        reconstruction.georef.utm_east_offset, 
                        reconstruction.georef.utm_north_offset
                    )
                else:
                    shutil.copy(obj_in, obj_out)
        
        #TODO: maybe parallelize this
        #TODO: gltf export? Should problably move the exporting process after this
        for texturing in [tree.odm_texturing, tree.odm_25dtexturing]:
            if reconstruction.multi_camera:
                primary = get_primary_band_name(reconstruction.multi_camera, args.primary_band)
                for band in reconstruction.multi_camera:
                    subdir = "" if band['name'] == primary else band['name'].lower()
                    obj_in = os.path.join(texturing, subdir, tree.odm_textured_model_obj_topo)
                    obj_out = os.path.join(texturing, subdir, tree.odm_textured_model_obj)
                    georefernce_textured_model(obj_in, obj_out)
            else:
                obj_in = os.path.join(texturing, tree.odm_textured_model_obj_topo)
                obj_out = os.path.join(texturing, tree.odm_textured_model_obj)
                transform_textured_model(obj_in, obj_out)
        
        if not io.file_exists(tree.odm_georeferencing_model_laz) or self.rerun():
            pipeline = pdal.Pipeline()
            pipeline |= pdal.Reader.ply(tree.filtered_point_cloud)
            pipeline |= pdal.Filter.ferry(dimensions="views => UserData")
            
            if reconstruction.is_georeferenced():
                log.ODM_INFO("Georeferencing point cloud")

                utmoffset = reconstruction.georef.utm_offset()
                pipeline |= pdal.Filter.transformation(
                    matrix=f"1 0 0 {utmoffset[0]} 0 1 0 {utmoffset[1]} 0 0 1 0 0 0 0 1"
                )

                # Establish appropriate las scale for export
                las_scale = 0.001
                filtered_point_cloud_stats = tree.path("odm_filterpoints", "point_cloud_stats.json")
                # Function that rounds to the nearest 10
                # and then chooses the one below so our
                # las scale is sensible
                def powerr(r):
                    return pow(10,round(math.log10(r))) / 10

                if os.path.isfile(filtered_point_cloud_stats):
                    try:
                        with open(filtered_point_cloud_stats, 'r') as stats:
                             las_stats = json.load(stats)
                             spacing = powerr(las_stats['spacing'])
                             log.ODM_INFO("las scale calculated as the minimum of 1/10 estimated spacing or %s, which ever is less." % las_scale)
                             las_scale = min(spacing, 0.001)
                    except Exception as e:
                        log.ODM_WARNING("Cannot find file point_cloud_stats.json. Using default las scale: %s" % las_scale)
                else:
                    log.ODM_INFO("No point_cloud_stats.json found. Using default las scale: %s" % las_scale)

                las_writer_def = {
                    "filename": tree.odm_georeferencing_model_laz,
                    "a_srs": reconstruction.georef.proj4(),
                    "offset_x": utmoffset[0],
                    "offset_y": utmoffset[1],
                    "offset_z": 0,
                    "scale_x": las_scale,
                    "scale_y": las_scale,
                    "scale_z": las_scale,
                }
                
                if reconstruction.has_gcp() and io.file_exists(gcp_geojson_zip_export_file):
                    if os.path.getsize(gcp_geojson_zip_export_file) <= 65535:
                        log.ODM_INFO("Embedding GCP info in point cloud")
                        las_writer_def["vlrs"] = json.dumps(
                            {
                                "filename": gcp_geojson_zip_export_file.replace(os.sep, "/"),
                                "user_id": "ODM",
                                "record_id": 2,
                                "description": "Ground Control Points (zip)"
                            }
                        )

                    else:
                        log.ODM_WARNING("Cannot embed GCP info in point cloud, %s is too large" % gcp_geojson_zip_export_file)

                pipeline |= pdal.Writer.las(
                    **las_writer_def
                )
    
                pipeline.execute()

                self.update_progress(50)

                if args.crop > 0:
                    log.ODM_INFO("Calculating cropping area and generating bounds shapefile from point cloud")
                    cropper = Cropper(tree.odm_georeferencing, 'odm_georeferenced_model')

                    if args.fast_orthophoto:
                        decimation_step = 4
                    else:
                        decimation_step = 40

                    # More aggressive decimation for large datasets
                    if not args.fast_orthophoto:
                        decimation_step *= int(len(reconstruction.photos) / 1000) + 1
                        decimation_step = min(decimation_step, 95)

                    try:
                        cropper.create_bounds_gpkg(tree.odm_georeferencing_model_laz, args.crop,
                                                    decimation_step=decimation_step)
                    except:
                        log.ODM_WARNING("Cannot calculate crop bounds! We will skip cropping")
                        args.crop = 0

                if 'boundary' in outputs and args.crop == 0:
                    log.ODM_INFO("Using boundary JSON as cropping area")

                    bounds_base, _ = os.path.splitext(tree.odm_georeferencing_model_laz)
                    bounds_json = bounds_base + ".bounds.geojson"
                    bounds_gpkg = bounds_base + ".bounds.gpkg"
                    export_to_bounds_files(outputs['boundary'], reconstruction.get_proj_srs(), bounds_json, bounds_gpkg)
            else:
                log.ODM_INFO("Converting point cloud (non-georeferenced)")
                pipeline |= pdal.Writer.las(
                    tree.odm_georeferencing_model_laz
                )
                pipeline.execute()


            stats_dir = tree.path("opensfm", "stats", "codem")
            if os.path.exists(stats_dir) and self.rerun():
                shutil.rmtree(stats_dir)

            if tree.odm_align_file is not None:
                alignment_file_exists = io.file_exists(tree.odm_georeferencing_alignment_matrix)

                if not alignment_file_exists or self.rerun():
                    if alignment_file_exists:
                        os.unlink(tree.odm_georeferencing_alignment_matrix)

                    a_matrix = None
                    try:
                        a_matrix = compute_alignment_matrix(tree.odm_georeferencing_model_laz, tree.odm_align_file, stats_dir)
                    except Exception as e:
                        log.ODM_WARNING("Cannot compute alignment matrix: %s" % str(e))

                    if a_matrix is not None:
                        log.ODM_INFO("Alignment matrix: %s" % a_matrix)

                        # Align point cloud
                        if os.path.isfile(unaligned_model):
                            os.rename(unaligned_model, tree.odm_georeferencing_model_laz)
                        os.rename(tree.odm_georeferencing_model_laz, unaligned_model)

                        try:
                            transform_point_cloud(unaligned_model, a_matrix, tree.odm_georeferencing_model_laz)
                            log.ODM_INFO("Transformed %s" % tree.odm_georeferencing_model_laz)
                        except Exception as e:
                            log.ODM_WARNING("Cannot transform point cloud: %s" % str(e))
                            os.rename(unaligned_model, tree.odm_georeferencing_model_laz)

                        # Align textured models
                        def transform_textured_model(obj):
                            if os.path.isfile(obj):
                                unaligned_obj = io.related_file_path(obj, postfix="_unaligned")
                                if os.path.isfile(unaligned_obj):
                                    os.rename(unaligned_obj, obj)
                                os.rename(obj, unaligned_obj)
                                try:
                                    transform_obj(unaligned_obj, a_matrix, [reconstruction.georef.utm_east_offset, reconstruction.georef.utm_north_offset], obj)
                                    log.ODM_INFO("Transformed %s" % obj)
                                except Exception as e:
                                    log.ODM_WARNING("Cannot transform textured model: %s" % str(e))
                                    os.rename(unaligned_obj, obj)
                        #TODO: seems gltf file is not converted in alignment?
                        for texturing in [tree.odm_texturing, tree.odm_25dtexturing]:
                            if reconstruction.multi_camera:
                                primary = get_primary_band_name(reconstruction.multi_camera, args.primary_band)
                                for band in reconstruction.multi_camera:
                                    subdir = "" if band['name'] == primary else band['name'].lower()
                                    obj = os.path.join(texturing, subdir, "odm_textured_model_geo.obj")
                                    transform_textured_model(obj)
                            else:
                                obj = os.path.join(texturing, "odm_textured_model_geo.obj")
                                transform_textured_model(obj)

                        with open(tree.odm_georeferencing_alignment_matrix, "w") as f:
                            f.write(np_to_json(a_matrix))
                    else:
                        log.ODM_WARNING("Alignment to %s will be skipped." % tree.odm_align_file)
                else:
                    log.ODM_WARNING("Already computed alignment")
            elif io.file_exists(tree.odm_georeferencing_alignment_matrix):
                os.unlink(tree.odm_georeferencing_alignment_matrix)

            point_cloud.post_point_cloud_steps(args, tree, self.rerun())
        else:
            log.ODM_WARNING('Found a valid georeferenced model in: %s'
                            % tree.odm_georeferencing_model_laz)

        if args.optimize_disk_space and io.file_exists(tree.odm_georeferencing_model_laz) and io.file_exists(tree.filtered_point_cloud):
            os.remove(tree.filtered_point_cloud)



