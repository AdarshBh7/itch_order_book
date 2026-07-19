"""Build and run the cocotb benches with icarus.

    python tb/run.py            # both benches
    python tb/run.py parser     # just the parser one
    ITCH_N_MSGS=50000 python tb/run.py   # more real data through the sim
"""

import os
import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

REPO = Path(__file__).resolve().parents[1]
RTL = [REPO / 'rtl' / 'itch_parser.sv',
       REPO / 'rtl' / 'order_book.sv',
       REPO / 'rtl' / 'book_top.sv']

TOOLS = Path('C:/Users/adars/tools/oss-cad-suite/bin')


def run(top, module, testcases=None):
    runner = get_runner('icarus')
    build_dir = REPO / 'sim_build' / top
    runner.build(sources=RTL, hdl_toplevel=top, build_dir=str(build_dir),
                 build_args=['-g2012'])
    # one vvp process per test so every test sees honest power on memory,
    # the level rams and way tables only zero at bitstream load
    for tc in (testcases or [None]):
        runner.test(hdl_toplevel=top, test_module=module,
                    testcase=tc, build_dir=str(build_dir),
                    test_dir=str(REPO / 'tb'))


def main():
    if TOOLS.exists():
        # the suite keeps its dlls in lib, both dirs have to be on PATH
        os.environ['PATH'] = str(TOOLS) + os.pathsep \
            + str(TOOLS.parent / 'lib') + os.pathsep + os.environ['PATH']
    os.environ.setdefault('PYTHONPATH', str(REPO / 'tb'))

    which = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if which in ('parser', 'all'):
        run('itch_parser', 'test_parser')
    if which in ('book', 'all'):
        run('book_top', 'test_book',
            testcases=['real_data_bbo', 'random_torture'])


if __name__ == '__main__':
    main()
