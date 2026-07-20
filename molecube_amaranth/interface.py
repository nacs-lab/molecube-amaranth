#

from amaranth import *
from amaranth_axi.axitools import axi_write_reg, AXISlaveReadIFace, AXISlaveWriteIFace

from transactron import TModule, Transaction
from transactron.lib import PipelineBuilder
from transactron.lib import BasicFifo

class ControlInterface(Elaboratable):
    def __init__(self, axi, csr_regs, fifos, ioctrl, prefix=0, valid_width=None):
        self.axi = axi
        self.addr_width = len(axi.AWADDR)
        self.data_width = len(axi.WDATA)
        assert self.data_width == 32
        self.id_width = len(axi.AWID)
        self.csr_regs = csr_regs
        self.fifos = fifos
        if valid_width is None:
            valid_width = self.addr_width
        self.prefix = prefix >> valid_width
        self.valid_width = valid_width
        self.ioctrl = ioctrl

    def elaborate(self, plat):
        m = TModule()

        m.submodules.write_iface = write_iface = AXISlaveWriteIFace(self.axi,
                                                                    buffered=True)
        m.submodules.read_iface = read_iface = AXISlaveReadIFace(self.axi,
                                                                 buffered=True)

        # m.submodules.write_pipe = write_pipe = PipelineBuilder()
        # start_write = write_pipe.create_external(i=[('idx', self.valid_width - 2),
        #                                             ('data', self.data_width),
        #                                             ('strb', 4)], o=[])

        # @write_pipe.stage(m)
        # def _(idx, data, strb):
        #     pass

        if self.valid_width != self.addr_width:
            m.submodules.prewrite_pipe = prewrite_pipe = PipelineBuilder()
            start_prewrite = prewrite_pipe.create_external(
                i=[('idx', self.addr_width - 2), ('data', self.data_width),
                   ('strb', 4), ('id', self.id_width), ('last', 1)], o=[])

            @prewrite_pipe.stage(m)
            def _(idx, data, strb, id, last):
                idx_prefix = idx >> (self.valid_width - 2)
                valid = Signal()
                m.d.top_comb += valid.eq(idx_prefix == self.prefix)
                with m.If(last):
                    write_iface.done(m, resp=Mux(valid, 0, 3), id=id)
                # with m.If(valid):
                #     start_write(m, idx=idx[:self.valid_width - 2], data=data, strb=strb)

        with Transaction().body(m):
            req = write_iface.get(m)
            addr = req.addr
            if self.valid_width == self.addr_width:
                # start_write(m, idx=addr >> 2, data=req.data, strb=req.strb)
                with m.If(req.last):
                    write_iface.done(m, id=req.id)
            else:
                start_prewrite(m, idx=addr >> 2, data=req.data, strb=req.strb,
                              id=req.id, last=req.last)

        m.submodules.read_pipe = read_pipe = PipelineBuilder()

        start_read = read_pipe.create_external(i=[('idx', self.addr_width - 2),
                                                  ('id', self.id_width),
                                                  ('last', 1)], o=[])

        # @read_pipe.stage(m)
        # def _():
        #     pass

        # read_pipe.fifo(depth=2)

        @read_pipe.stage(m, o=[('idx', self.valid_width - 2), ('resp', 2)])
        def _(idx):
            return dict(idx=idx[:self.valid_width - 2],
                        resp=Mux((idx >> (self.valid_width - 2)) == self.prefix, 0, 3))

        @read_pipe.stage(m, o=[(f'idx{i}', 1) for i in range(self.valid_width - 2)])
        def _(idx):
            return {f'idx{i}': idx[i] for i in range(self.valid_width - 2)}

        read_regs = {
0: C(0, 32),
1: C(0, 32),
2: C(0, 32),
3: C(0, 32),
4: C(0, 32),
5: C(0, 32),
6: C(0, 32),
7: C(0, 32),
8: C(0, 32),
9: C(0, 32),
10: C(0, 32),
11: C(0, 32),
12: C(0, 32),
13: C(0, 32),
14: C(0, 32),
15: C(0, 32),
16: C(0, 32),
17: C(0, 32),
18: C(0, 32),
19: C(0, 32),
20: C(0, 32),
21: C(0, 32),
22: C(0, 32),
23: C(0, 32),
24: C(0, 32),
25: C(0, 32),
26: C(0, 32),
27: C(0, 32),
28: C(0, 32),
29: C(0, 32),
30: C(0, 32),
31: C(0, 32),
32: C(0, 32),
33: C(0, 32),
34: C(0, 32),
35: C(0, 32),
36: C(0, 32),
37: C(0, 32),
38: C(0, 32),
39: C(0, 32),
40: C(0, 32),
41: C(0, 32),
42: C(0, 32),
43: C(0, 32),
44: C(0, 32),
45: C(0, 32),
46: C(0, 32),
47: C(0, 32),
48: C(0, 32),
49: C(0, 32),
50: C(0, 32),
51: C(0, 32),
52: C(0, 32),
53: C(0, 32),
54: C(0, 32),
55: C(0, 32),
56: C(0, 32),
57: C(0, 32),
58: C(0, 32),
59: C(0, 32),
60: C(0, 32),
61: C(0, 32),
62: C(0, 32),
63: C(0, 32),
        }

        stage_state = {k: lambda arg, v=v: v for k, v in read_regs.items()}
        # stage_state[0x1f] = lambda arg: arg.fifo_data

        def get_stage(arg, i):
            if i in stage_state:
                return stage_state[i](arg)

        max_batch_sz = 1024
        for bit in range(self.valid_width - 2):
            idx_out_width = self.valid_width - 2 - 1 - bit
            next_stage_state = {}
            for idx_out_val in range(1 << idx_out_width):
                if ((idx_out_val * 2) in stage_state or
                    (idx_out_val * 2 + 1) in stage_state):
                    fld = f'data_{bit}_{idx_out_val}'
                    next_stage_state[idx_out_val] = lambda arg, fld=fld: getattr(arg, fld)

            idx_outs = list(next_stage_state.keys())
            nidx_outs = len(idx_outs)
            nbatches = (nidx_outs + max_batch_sz - 1) // max_batch_sz
            batch_sz = (nidx_outs + nbatches - 1) // nbatches

            if bit == 2:
                read_pipe.fifo(depth=2)

            print(f"bit: {bit}, idx_out_width: {idx_out_width}, nidx_outs: {nidx_outs}")

            for start_idx in range(0, nidx_outs, batch_sz):
                end_idx = min(nidx_outs, start_idx + batch_sz)
                idxs = idx_outs[start_idx:end_idx]

                layout_in = [(f'idx{bit}', 1)]
                layout_out = []

                for idx_out_val in idxs:
                    if idx_out_width == 0:
                        layout_out.append(('data', self.data_width))
                    else:
                        layout_out.append((f'data_{bit}_{idx_out_val}', self.data_width))
                    if bit == 0:
                        continue
                    if (idx_out_val * 2) in stage_state:
                        layout_in.append((f'data_{bit - 1}_{idx_out_val * 2}', self.data_width))
                    if (idx_out_val * 2 + 1) in stage_state:
                        layout_in.append((f'data_{bit - 1}_{idx_out_val * 2 + 1}', self.data_width))

                @read_pipe.stage(m, i=layout_in, o=layout_out)
                def _(arg):
                    res = {}
                    idx_bit = getattr(arg, f'idx{bit}')
                    for idx_out_val in idxs:
                        v0 = get_stage(arg, idx_out_val * 2)
                        v1 = get_stage(arg, idx_out_val * 2 + 1)
                        if idx_out_width == 0:
                            fld = 'data'
                        else:
                            fld = f'data_{bit}_{idx_out_val}'
                        if v0 is None:
                            res[fld] = v1
                        elif v1 is None:
                            res[fld] = v0
                        else:
                            res[fld] = Mux(idx_bit, v1, v0)
                    return res

            stage_state = next_stage_state

        @read_pipe.stage(m)
        def _(data, resp, id, last):
            read_iface.done(m, data=data, resp=resp, id=id, last=last)

        with Transaction().body(m):
            req = read_iface.get(m)
            start_read(m, idx=req.addr >> 2, id=req.id, last=req.last)

        return m
