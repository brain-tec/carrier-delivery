# -*- coding: utf-8 -*-
# Copyright 2013-2016 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl)
import Queue
import logging
import openerp
import threading
from contextlib import contextmanager
from itertools import groupby
from openerp import _, api, exceptions, fields, models, tools

from ..pdf_utils import assemble_pdf
from ..zpl_utils import assemble_zpl2

_logger = logging.getLogger(__name__)


class DeliveryCarrierLabelGenerate(models.TransientModel):

    _name = 'delivery.carrier.label.generate'

    @api.multi
    def _get_batch_ids(self):
        res = False
        if (self.env.context.get('active_model') == 'stock.batch.picking' and
                self.env.context.get('active_ids')):
            res = self.env.context['active_ids']
        return res

    batch_ids = fields.Many2many(
        'stock.batch.picking',
        string='Picking Batch',
        default=_get_batch_ids)
    generate_new_labels = fields.Boolean(
        'Generate new labels',
        default=False,
        help="If this option is used, new labels will be "
             "generated for the packs even if they already have one.\n"
             "The default is to use the existing label.")

    @api.model
    def _get_packs(self, batch):
        operations = batch.pack_operation_ids
        operations = sorted(
            operations,
            key=lambda r: r.result_package_id.name or r.package_id.name
        )
        for pack, grp_operations in groupby(
                operations,
                key=lambda r: r.result_package_id or r.package_id):
            pack_label = self._find_pack_label(pack)
            yield pack, list(grp_operations), pack_label

    @api.model
    def _find_picking_label(self, picking):
        label_obj = self.env['shipping.label']
        domain = [
            ('res_id', '=', picking.id),
            ('package_id', '=', False),
        ]
        return label_obj.search(
            domain, order='create_date DESC', limit=1
        )

    @api.model
    def _find_pack_label(self, pack):
        label_obj = self.env['shipping.label']
        domain = [('package_id', '=', pack.id)]
        return label_obj.search(
            domain, order='create_date DESC', limit=1
        )

    @contextmanager
    @api.model
    def _do_in_new_env(self):
        if tools.config["test_enable"]:
            yield self.env
            raise StopIteration
        with openerp.api.Environment.manage():
            with openerp.registry(self.env.cr.dbname).cursor() as new_cr:
                yield openerp.api.Environment(new_cr, self.env.uid,
                                              self.env.context)

    def _do_generate_labels(self, group):
        """ Generate a label in a thread safe context
        Here we declare a specific cursor so do not launch
        too many threads
        """
        self.ensure_one()

        # create a cursor to be thread safe
        with self._do_in_new_env() as new_env:
            for pack, picking, label in group:
                # generate the label of the pack
                package_ids = [pack.id] if pack else None
                try:
                    picking.with_env(new_env).generate_labels(
                        package_ids=package_ids
                    )
                except Exception as e:
                    # add information on picking and pack in the exception
                    picking_name = _('Picking: %s') % picking.name
                    pack_num = _('Pack: %s') % pack.name if pack else ''
                    # pylint: disable=translation-required
                    raise exceptions.UserError(
                        ('%s %s - %s') % (picking_name, pack_num, e)
                    )

    def _worker(self, data_queue, error_queue):
        """ A worker to generate labels
        Takes data from queue data_queue
        And if the worker encounters errors, he will add them in
        error_queue queue
        """
        self.ensure_one()
        while not data_queue.empty():
            try:
                group = data_queue.get()
            except Queue.Empty:
                return
            try:
                self._do_generate_labels(group)
            except Exception as e:
                error_queue.put(e)
            finally:
                data_queue.task_done()

    @api.model
    def _get_num_workers(self):
        """ Get number of worker parameter for labels generation
        Optional ir.config_parameter is `shipping_label.num_workers`
        """
        param_model = self.env['ir.config_parameter']
        num_workers = param_model.get_param('shipping_label.num_workers')
        if not num_workers:
            return 1
        return int(num_workers)

    @api.multi
    def _get_all_files(self, batch):
        self.ensure_one()

        data_queue = Queue.Queue()
        error_queue = Queue.Queue()

        # If we have more than one pack in a picking, we must ensure
        # they are not executed concurrently or we will have concurrent
        # transaction errors. So we process them in the same thread.
        # We put them in the same 'group' and this group will be passed
        # as a whole to a thread worker.
        groups = {}
        for pack, operations, label in self._get_packs(batch):
            if not label or self.generate_new_labels:
                picking = operations[0].picking_id
                groups.setdefault(picking.id, []).append(
                    (pack, picking, label)
                )

        for group in groups.itervalues():
            data_queue.put(group)

        # create few workers to parallelize label generation
        num_workers = self._get_num_workers()
        _logger.info('Starting %s workers to generate labels', num_workers)
        for i in range(num_workers):
            t = threading.Thread(target=self._worker,
                                 args=(data_queue, error_queue))
            t.daemon = True
            t.start()

        # wait for all tasks to be done
        data_queue.join()

        # We will not create a partial PDF if some labels weren't
        # generated thus we raise catched exceptions by the workers
        # We will try to regroup all orm exception in one
        if not error_queue.empty():

            error_count = {}
            messages = []
            while not error_queue.empty():
                e = error_queue.get()
                if isinstance(e, exceptions.UserError):
                    if e.name not in error_count:
                        error_count[e.name] = 1
                    else:
                        error_count[e.name] += 1
                    messages.append(unicode(e) or '')
                else:
                    # raise other exceptions like PoolError if
                    # too many cursor where created by workers
                    raise e
            titles = []
            for key, v in error_count.iteritems():
                titles.append('%sx %s' % (v, key))

            message = _('Some labels couldn\'t be generated. Please correct '
                        'following errors and generate labels again to create '
                        'the ones which failed.\n\n'
                        ) + '\n'.join(messages)
            raise exceptions.UserError(message)

        # create a new cursor to be up to date with what was created by workers
        with self._do_in_new_env() as new_env:
            # labels = new_env['shipping.label']
            self_env = self.with_env(new_env)
            labels = []
            for pack, operations, label in self_env._get_packs(batch):
                picking = operations[0].picking_id
                if pack:
                    label = self_env._find_pack_label(pack)
                else:
                    label = self_env._find_picking_label(picking)
                if not label:
                    continue
                labels.append((label.file_type, label.attachment_id.datas))
            return labels

    @api.multi
    def action_generate_labels(self):
        """
        Call the creation of the delivery carrier label
        of the missing labels and get the existing ones
        Then merge all of them in a single PDF

        """
        self.ensure_one()
        if not self.batch_ids:
            raise exceptions.UserError(_('No picking batch selected'))

        to_generate = self.batch_ids
        if not self.generate_new_labels:
            already_generated_ids = self.env['ir.attachment'].search(
                [('res_model', '=', 'stock.batch.picking'),
                 ('res_id', 'in', self.batch_ids.ids)]
            ).mapped('res_id')
            to_generate = to_generate.filtered(
                lambda rec: rec.id not in already_generated_ids
            )

        for batch in to_generate:
            labels = self._get_all_files(batch)
            labels_by_f_type = self._group_labels_by_file_type(labels)
            for f_type, labels in labels_by_f_type.iteritems():
                labels_bin = [
                    label.decode("base64") for label in labels if label
                ]
                filename = batch.name + '.' + f_type
                filedata = self._concat_files(f_type, labels_bin)
                if not filedata:
                    # Merging of `f_type` not supported, so we cannot
                    # create the attachment
                    continue
                data = {
                    'name': filename,
                    'res_id': batch.id,
                    'res_model': 'stock.batch.picking',
                    'datas': filedata.encode("base64"),
                    'datas_fname': filename,
                }
                self.env['ir.attachment'].create(data)

        return {
            'type': 'ir.actions.act_window_close',
        }

    @api.model
    def _group_labels_by_file_type(self, labels):
        res = {}
        for f_type, label in labels:
            res.setdefault(f_type, [])
            res[f_type].append(label)
        return res

    @api.model
    def _concat_files(self, file_type, files):
        if file_type == 'pdf':
            return assemble_pdf(files)
        if file_type == 'zpl2':
            return assemble_zpl2(files)
        # Merging files of `file_type` not supported, we return nothing
        return
