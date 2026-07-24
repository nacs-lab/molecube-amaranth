#

from amaranth import *
from amaranth.lib.data import Layout, View

import random

_FILENAME = "_xvalue_source.v"

_FILECONTENT = """
module _XVALUE_GENERATOR #(parameter VALUE_WIDTH = 1)
    (output wire [VALUE_WIDTH-1:0] value);
    generate
        genvar i;
        for (i = 0; i < VALUE_WIDTH; i = i + 1) begin: assign_xvalue
            assign value[i] = 1'bx;
        end
    endgenerate
endmodule
"""

class _XValueGenerator(Elaboratable):
    def __init__(self, value):
        self.value = value

    def elaborate(self, plat):
        value = Value.cast(self.value)
        width = len(value)

        if plat is None:
            m = Module()

            randval = Signal.like(value, init=random.randint(0, (1 << width) - 1))
            m.d.comb += value.eq(randval)

            series_len = 103
            index = Signal(range(series_len))
            m.d.sync += index.eq((index + 1) % series_len)
            with m.Switch(index):
                for i in range(series_len):
                    with m.Case(i):
                        m.d.sync += randval.eq(random.randint(0, (1 << width) - 1))

            return m

        if _FILENAME not in plat.extra_files:
            plat.add_file(_FILENAME, _FILECONTENT)

        return Instance(
            '_XVALUE_GENERATOR',
            a_KEEP_HIERARCHY="true", # Try to prevent decloning of module
            p_VALUE_WIDTH=width,
            o_value=value,
        )

def xvalue(m, T):
    gen = _XValueGenerator(Signal(T))
    m.submodules += gen
    return gen.value

def assign_xvalue(m, s, *, domain='sync'):
    gen = _XValueGenerator(Signal.like(s))
    m.submodules += gen
    m.d[domain] += s.eq(gen.value)

def oring_combiner(m, args, runs):
    arg0 = args[0]
    shape = arg0.shape()
    res = Mux(runs[0], Value.cast(arg0), 0)
    for i, v in enumerate(args):
        if i == 0:
            continue
        res = res | Mux(runs[i], Value.cast(v), 0)
    return View(shape, res)

Concat = type(Cat(Signal(), Signal()))
Slice = type(Signal()[:])

def get_init(obj):
    obj = Value.cast(obj)
    if type(obj) is Const:
        return obj
    elif type(obj) is Signal:
        return Const(obj.init, len(obj))
    elif type(obj) is Concat:
        return Cat(get_init(part) for part in obj.parts)
    elif type(obj) is Slice:
        return get_init(obj.value)[obj.start:obj.stop]
    else:
        raise TypeError(f"Cannot get init value for {obj!r}")

class RegChain(Elaboratable):
    def __init__(self, output, input, levels, *, reset_less=False):
        self.input = input
        self.output = output
        self.levels = levels
        self.reset_less = reset_less

    def elaborate(self, plat):
        m = Module()

        if self.levels == 0:
            m.d.comb += self.output.eq(self.input)
        else:
            src = self.input
            for _ in range(self.levels - 1):
                tgt = Signal.like(src, init=get_init(src), reset_less=self.reset_less)
                m.d.sync += tgt.eq(src)
                src = tgt
            m.d.sync += self.output.eq(src)

        return m

def reg_chain(m, *, input=None, levels, output=None, reset_input=True, reset_mid=True, reset_output=True):
    if input is None and output is None:
        raise TypeError("At least one of input and output must be provided")
    if levels == 0:
        if output is None:
            return input, input
        if input is None:
            return output, output
    if output is None:
        output = Signal.like(input, reset_less=not reset_output)
    elif input is None:
        input = Signal.like(output, init=get_init(output), reset_less=not reset_input)
    m.submodules += RegChain(output, input, levels, reset_less=not reset_mid)
    return output, input
