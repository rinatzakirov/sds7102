#! /usr/bin/python
import hacking
if __name__ == '__main__':
    hacking.reexec_if_needed('image.py')

from pprint import pprint

from myhdl import (Signal, ResetSignal, TristateSignal, ConcatSignal,
                   intbv, always_comb, always_seq)
from rhea.cores.misc import syncro

from spartan6 import (startup_spartan6, bufg,
                      ibufds, ibufgds, ibufgds_diff_out, ibufds_vec, iddr2)

from system import System
from spi_slave import SpiInterface, SpiSlave
from wb import WbMux
from hybrid_counter import HybridCounter
from util import tristate
from regfile import RegFile, Field, RoField, RwField, Port
from ddr import Ddr, DdrBus, ddr_connect
from simplebus import SimpleBus, SimpleMux, SimpleAlgo, SimpleRam
from sampler import Sampler, MigSampler
from shifter import Shifter, ShifterBus
from ram import Ram
from mig import Mig, MigPort, mig_with_tb
from frontpanel import FrontPanel

from simplebus import SimpleReg
from simplebus import RwField as SimpleRwField
from simplebus import RoField as SimpleRoField
from simplebus import DummyField as SimpleDummyField

def top(din, init_b, cclk,
        ref_clk,
        soc_clk_p, soc_clk_n, soc_cs, soc_ras, soc_cas, soc_we, soc_ba, soc_a,
        soc_dqs, soc_dm, soc_dq,
        adc_clk_p, adc_clk_n, adc_dat_p, adc_dat_n, adc_ovr_p, adc_ovr_n,
        shifter_sck, shifter_sdo,
        bu2506_ld, adf4360_le, adc08d500_cs, lmh6518_cs, dac8532_sync,
        trig_p, trig_n, ba7406_vd, ba7406_hd, ac_trig,
        probe_comp, ext_trig_out,
        i2c_scl, i2c_sda,
        fp_rst, fp_clk, fp_din, led_green, led_white,
        mcb3_dram_ck, mcb3_dram_ck_n,
        mcb3_dram_ras_n, mcb3_dram_cas_n, mcb3_dram_we_n,
        mcb3_dram_ba, mcb3_dram_a, mcb3_dram_odt,
        mcb3_dram_dqs, mcb3_dram_dqs_n, mcb3_dram_udqs, mcb3_dram_udqs_n,
        mcb3_dram_dm, mcb3_dram_udm, mcb3_dram_dq,
        bank2):
    insts = []

    # Clock generator using STARTUP_SPARTAN primitive
    clk_unbuf = Signal(False)
    clk_unbuf_inst = startup_spartan6('startup_inst', cfgmclk = clk_unbuf)
    insts.append(clk_unbuf_inst)

    clk_buf = Signal(False)
    clk_inst = bufg('bufg_clk', clk_unbuf, clk_buf)
    insts.append(clk_inst)

    spi_system = System(clk_buf, None)

    mux = WbMux()

    # Rename and adapt external SPI bus signals
    slave_spi_bus = SpiInterface()
    slave_cs = Signal(False)
    slave_cs_inst = syncro(clk_buf, din, slave_spi_bus.CS)
    insts.append(slave_cs_inst)
    slave_sck_inst = syncro(clk_buf, cclk, slave_spi_bus.SCK)
    insts.append(slave_sck_inst)
    slave_sdioinst = tristate(init_b,
                              slave_spi_bus.SD_I,
                              slave_spi_bus.SD_O, slave_spi_bus.SD_OE)
    insts.append(slave_sdioinst)

    ####################################################################
    # A MUX and some test code for it

    soc_clk = Signal(False)
    soc_clk_b = Signal(False)

    soc_system = System(soc_clk, None)

    sm = SimpleMux(soc_system)

    if 1:
        # Some RAM
        sr = SimpleRam(soc_system, 1024, 32)
        sr_inst = sr.gen()
        insts.append(sr_inst)
        sm.add(sr.bus(), addr = 0x8000)

    if 1:
        # A read only area which returns predictable patterns
        sa = SimpleAlgo(soc_system, (1<<16), 32)
        sa_inst = sa.gen()
        insts.append(sa_inst)
        sm.add(sa.bus(), 0x10000)

    ####################################################################
    # MIG control and status

    mig_rst = ResetSignal(val = False, active = True, async = True)
    mig_calib_done = Signal(False)

    # MIG control register
    mig_control = SimpleReg(soc_system, 'mig_control', "MIG control", [
        SimpleRwField('reset', "Reset", mig_rst),
        SimpleRoField('calib_done', "Calib Done", mig_calib_done),
        ])
    sm.add(mig_control.bus(), addr = 0x200)
    insts.append(mig_control.gen())

    mig_ports = [ None ] * 6

    ####################################################################
    # MIG port 0

    mig_port = MigPort(soc_system.CLK)
    mig_ports[0] = mig_port

    # MIG port 0 command register

    # I'm trying to squeeze all the MIG command bits into a 32 bit
    # register.  Since the MIG port is 32 bits wide, the low two
    # address bits are always going to be zero, so we only have to
    # keep track of the highest 24 bits of a 32 MByte address.  The
    # highest bit of the instr field is only 1 when used for refresh.
    # I won't use refresh so I can skip that bit too.

    mig_cmd_addr = Signal(intbv(0)[24:])
    mig_cmd_instr = Signal(intbv(0)[2:])

    @always_comb
    def mig_cmd_comb():
        mig_port.cmd_byte_addr.next = mig_cmd_addr << 2
        mig_port.cmd_instr.next = mig_cmd_instr
    insts.append(mig_cmd_comb)

    mig_cmd = SimpleReg(soc_system, 'mig_cmd', "MIG cmd", [
        SimpleRwField('addr', "", mig_cmd_addr),
        SimpleRwField('bl', "", mig_port.cmd_bl),
        SimpleRwField('instr', "", mig_cmd_instr),
        ])
    sm.add(mig_cmd.bus(), addr = 0x210)
    insts.append(mig_cmd.gen())

    # Strobe the cmd_en signal after the register has been written
    mig_cmd_bus = mig_cmd.bus()
    @always_seq(soc_clk.posedge, soc_system.RST)
    def mig_cmd_seq():
        mig_port.cmd_en.next = mig_cmd_bus.WR
    insts.append(mig_cmd_seq)

    # MIG port 0 status register, all the status bits from cmd, wr and rd
    mig_status = SimpleReg(soc_system, 'mig_ctrl', "DRAM ctrl", [
        SimpleRoField('rd_count', "", mig_port.rd_count),
        SimpleDummyField(1),
        SimpleRoField('rd_empty', "", mig_port.rd_empty),
        SimpleRoField('rd_full', "", mig_port.rd_full),
        SimpleRoField('rd_error', "", mig_port.rd_error),
        SimpleRoField('rd_overflow', "", mig_port.rd_overflow),
        SimpleRoField('wr_count', "", mig_port.wr_count),
        SimpleDummyField(1),
        SimpleRoField('wr_empty', "", mig_port.wr_empty),
        SimpleRoField('wr_full', "", mig_port.wr_full),
        SimpleRoField('wr_error', "", mig_port.wr_error),
        SimpleRoField('wr_underrun', "", mig_port.wr_underrun),
        SimpleRoField('cmd_empty', "", mig_port.cmd_empty),
        SimpleRoField('cmd_full', "", mig_port.cmd_full),
        ])
    sm.add(mig_status.bus(), addr = 0x211)
    insts.append(mig_status.gen())

    # MIG port 0 counts, just a count of MIG read/write strobes to
    # help me debug the SoC side of things.
    mig_rd_count = Signal(intbv(0)[16:])
    mig_wr_count = Signal(intbv(0)[16:])

    mig_counts = SimpleReg(soc_system, 'mig_counts', "MIG counts", [
        SimpleRoField('rd_count', "", mig_rd_count),
        SimpleRoField('wr_count', "", mig_wr_count),
        ])
    sm.add(mig_counts.bus(), addr = 0x212)
    insts.append(mig_counts.gen())

    @always_seq(soc_system.CLK.posedge, soc_system.RST)
    def mig_counts_seq():
        if mig_rst:
            mig_rd_count.next = 0
            mig_wr_count.next = 0
        else:
            if mig_port.rd_en:
                mig_rd_count.next = mig_rd_count + 1
            if mig_port.wr_en:
                mig_wr_count.next = mig_wr_count + 1
    insts.append(mig_counts_seq)

    # MIG port 0 read/write data.  This register must be a bit off
    # from any other regiters that might be read to avoid a read burst
    # popping off data from the fifo when not expected
    mig_data_bus = SimpleBus(1, 32)
    sm.add(mig_data_bus, addr = 0x218)

    @always_comb
    def mig_data_comb():
        mig_port.wr_en.next = mig_data_bus.WR
        mig_port.wr_data.next = mig_data_bus.WR_DATA
        mig_port.rd_en.next = mig_data_bus.RD
    insts.append(mig_data_comb)

    @always_seq (soc_system.CLK.posedge, soc_system.RST)
    def mig_data_seq():
        if mig_data_bus.RD:
            mig_data_bus.RD_DATA.next = mig_port.rd_data
        else:
            mig_data_bus.RD_DATA.next = 0
    insts.append(mig_data_seq)

    ####################################################################
    # Front panel attached to the SoC bus

    if 1:
        frontpanel = FrontPanel(soc_system, fp_rst, fp_clk, fp_din)
        frontpanel_inst = frontpanel.gen()
        insts.append(frontpanel_inst)

        # These need to be spaced a bit apart, otherwise burst will make
        # us read from the data_bus register when we only want to read the
        # ctl_bus register.
        sm.add(frontpanel.ctl_bus, addr = 0x100)
        sm.add(frontpanel.data_bus, addr = 0x104)

    ####################################################################
    # LEDs on the front panel

    if 1:
        led_green_tmp = Signal(False)
        led_white_tmp = Signal(False)

        misc_reg = SimpleReg(soc_system, 'misc', "Miscellaneous", [
            SimpleRwField('green', "Green LED", led_green_tmp),
            SimpleRwField('white', "White LED", led_white_tmp),
            ])

        sm.add(misc_reg.bus(), addr = 0x108)
        insts.append(misc_reg.gen())

        @always_comb
        def led_inst():
            led_green.next = led_green_tmp
            led_white.next = led_white_tmp
        insts.append(led_inst)

    ####################################################################
    # ADC bus

    adc_clk = Signal(False)
    adc_clk_b = Signal(False)
    adc_clk_ibuf_inst = ibufgds_diff_out('ibufgds_diff_out_adc_clk',
                                         adc_clk_p, adc_clk_n,
                                         adc_clk, adc_clk_b)
    insts.append(adc_clk_ibuf_inst)

    adc_clk._name = 'adc_clk' # Must match name of timing spec in ucf file
    adc_clk_b._name = 'adc_clk_b' # Must match name of timing spec in ucf file

    adc_dat = Signal(intbv(0)[len(adc_dat_p):])
    adc_dat_ibuf_inst = ibufds_vec('adc_dat_ibufds',
                                   adc_dat_p, adc_dat_n, adc_dat)
    insts.append(adc_dat_ibuf_inst)

    adc_dat_0 = Signal(intbv(0)[len(adc_dat):])
    adc_dat_1 = Signal(intbv(0)[len(adc_dat):])
    adc_dat_ddr_inst = iddr2('adc_dat_iddr2',
                             adc_dat, adc_dat_0, adc_dat_1,
                             c0 = adc_clk, c1 = adc_clk_b,
                             ddr_alignment = 'C0')
    insts.append(adc_dat_ddr_inst)

    adc_ovr = Signal(False)
    adc_ovr_inst = ibufds('ibufds_adc_ovr',
                          adc_ovr_p, adc_ovr_n, adc_ovr)
    insts.append(adc_ovr_inst)

    if 1:
        fifo_overflow_0 = Signal(False)
        cmd_overflow_0 = Signal(False)
        fifo_overflow_1 = Signal(False)
        cmd_overflow_1 = Signal(False)

        adc_capture = Signal(False)
        adc_ctl = RegFile('adc_ctl', "ADC control", [
            RwField(spi_system, 'adc_capture', "Capture samples", adc_capture),
            RoField(spi_system, 'fifo_overflow_0', "", fifo_overflow_0),
            RoField(spi_system, 'cmd_overflow_0', "", cmd_overflow_0),
            RoField(spi_system, 'fifo_overflow_', "", fifo_overflow_1),
            RoField(spi_system, 'cmd_overflow_1', "", cmd_overflow_1),
            ])
        mux.add(adc_ctl, 0x230)

        adc_capture_sync = Signal(False)
        adc_capture_sync_inst = syncro(adc_clk, adc_capture, adc_capture_sync)
        insts.append(adc_capture_sync_inst)

    if 1:
        adc_sampler_0 = Sampler(addr_depth = 1024,
                                sample_clk = adc_clk,
                                sample_data = adc_dat_0,
                                sample_enable = adc_capture_sync,
                                skip_cnt = 99)
        mux.add(adc_sampler_0, 0x4000)

        adc_sampler_1 = Sampler(addr_depth = 1024,
                                sample_clk = adc_clk,
                                sample_data = adc_dat_1,
                                sample_enable = adc_capture_sync,
                                skip_cnt = 99)
        mux.add(adc_sampler_1, 0x6000)

    if 1:
        adc_mig_port_0 = MigPort(adc_clk)
        mig_ports[2] = adc_mig_port_0
        mig_sampler_0 = MigSampler(port = adc_mig_port_0,
                                   base = 32, chunk = 32, stride = 64,
                                   count = 256 * 1024,
                                   sample_clk = adc_clk,
                                   sample_data = adc_dat_0,
                                   sample_enable = adc_capture_sync,
                                   fifo_overflow = fifo_overflow_0,
                                   cmd_overflow = cmd_overflow_0)
        insts.append(mig_sampler_0.gen())

        adc_mig_port_1 = MigPort(adc_clk)
        mig_ports[3] = adc_mig_port_1
        mig_sampler_1 = MigSampler(port = adc_mig_port_1,
                                   base = 0, chunk = 32, stride = 64,
                                   count = 256 * 1024,
                                   sample_clk = adc_clk,
                                   sample_data = adc_dat_1,
                                   sample_enable = adc_capture_sync,
                                   fifo_overflow = fifo_overflow_1,
                                   cmd_overflow = cmd_overflow_1)
        insts.append(mig_sampler_1.gen())

    ####################################################################
    # Analog frontend

    if 1:
        shifter_bus = ShifterBus(6)

        @always_comb
        def shifter_comb():
            shifter_sck.next = shifter_bus.SCK
            shifter_sdo.next = shifter_bus.SDO

            bu2506_ld.next = shifter_bus.CS[0]
            adf4360_le.next = shifter_bus.CS[1]
            adc08d500_cs.next = not shifter_bus.CS[2]
            lmh6518_cs.next[0] = not shifter_bus.CS[3]
            lmh6518_cs.next[1] = not shifter_bus.CS[4]
            dac8532_sync.next = not shifter_bus.CS[5]
        insts.append(shifter_comb)

        shifter = Shifter(spi_system, shifter_bus, divider = 100)
        addr = 0x210
        for reg in shifter.create_regs():
            mux.add(reg, addr)
            addr += 1
        insts.append(shifter.gen())

    trig = Signal(intbv(0)[len(trig_p):])
    trig_inst = ibufds_vec('ibufds_trig', trig_p, trig_n, trig)
    insts.append(trig_inst)

    ####################################################################
    # Probe compensation output and external trigger output
    # Just toggle them at 1kHz

    probe_comb_div = 25000
    probe_comp_ctr = Signal(intbv(0, 0, probe_comb_div))
    probe_comp_int = Signal(False)
    @always_seq (spi_system.CLK.posedge, spi_system.RST)
    def probe_comp_seq():
        if probe_comp_ctr == probe_comb_div - 1:
            probe_comp_int.next = not probe_comp_int
            probe_comp_ctr.next = 0
        else:
            probe_comp_ctr.next = probe_comp_ctr + 1
    insts.append(probe_comp_seq)
    @always_comb
    def probe_comp_comb():
        probe_comp.next = probe_comp_int
        ext_trig_out.next = probe_comp_int
    insts.append(probe_comp_comb)

    ####################################################################
    # DDR memory using MIG

    # The DDR memory controller uses the SoC clock pins as the input
    # to its PLL.  It also generates soc_clk which is used above

    soc_clk_ibuf = Signal(False)
    soc_clk_ibuf_inst = ibufgds('soc_clk_ibuf_inst',
                                soc_clk_p, soc_clk_n,
                                soc_clk_ibuf)
    insts.append(soc_clk_ibuf_inst)

    mig = Mig()
    mig.rst = mig_rst
    mig.clkin = soc_clk_ibuf

    @always_comb
    def mig_soc_clk_inst():
        soc_clk.next = mig.soc_clk
        soc_clk_b.next = mig.soc_clk_b
    insts.append(mig_soc_clk_inst)

    mig.calib_done = mig_calib_done

    mig.mcbx_dram_addr = mcb3_dram_a
    mig.mcbx_dram_ba = mcb3_dram_ba
    mig.mcbx_dram_ras_n = mcb3_dram_ras_n
    mig.mcbx_dram_cas_n = mcb3_dram_cas_n
    mig.mcbx_dram_we_n = mcb3_dram_we_n
    mig.mcbx_dram_clk = mcb3_dram_ck
    mig.mcbx_dram_clk_n = mcb3_dram_ck_n
    mig.mcbx_dram_dq = mcb3_dram_dq
    mig.mcbx_dram_dqs = mcb3_dram_dqs
    mig.mcbx_dram_dqs_n = mcb3_dram_dqs_n
    mig.mcbx_dram_udqs = mcb3_dram_udqs
    mig.mcbx_dram_udqs_n = mcb3_dram_udqs_n
    mig.mcbx_dram_udm = mcb3_dram_udm
    mig.mcbx_dram_ldm = mcb3_dram_dm

    mig.ports = mig_ports

    mig_inst = mig.gen()
    insts.append(mig_inst)

    ####################################################################
    # Finalize the SoC MUX
    sm.addr_depth = 32 * 1024 * 1024
    sm_inst = sm.gen()
    insts.append(sm_inst)

    ####################################################################
    # SoC bus

    soc_bus = DdrBus(2, 12, 2)

    # Attach the MUX bus to the SoC bus
    soc_ddr = Ddr()
    soc_inst = soc_ddr.gen(soc_system, soc_bus, sm.bus())
    insts.append(soc_inst)

    soc_connect_inst = ddr_connect(
        soc_bus, soc_clk, soc_clk_b, None,
        soc_cs, soc_ras, soc_cas, soc_we, soc_ba, soc_a,
        soc_dqs, soc_dm, soc_dq)
    insts.append(soc_connect_inst)

    if 1:

        soc_capture = Signal(False)
        soc_ctl = RegFile('soc_ctl', "SOC control", [
            RwField(spi_system, 'soc_capture', "Capture samples", soc_capture),
            ])
        mux.add(soc_ctl, 0x231)
        soc_capture_sync = Signal(False)
        soc_capture_sync_inst = syncro(soc_clk, soc_capture, soc_capture_sync)
        insts.append(soc_capture_sync_inst)

        soc_sdr = ConcatSignal(
            soc_a, soc_ba, soc_we, soc_cas, soc_ras, soc_cs)

        soc_sdr_sampler = Sampler(addr_depth = 0x800,
                                  sample_clk = soc_clk,
                                  sample_data = soc_sdr,
                                  sample_enable = soc_capture_sync)
        mux.add(soc_sdr_sampler, 0x2000)

        soc_reg = ConcatSignal(
            soc_bus.A, soc_bus.BA,
            soc_bus.WE_B, soc_bus.CAS_B, soc_bus.RAS_B, soc_bus.CS_B)

        soc_reg_sampler = Sampler(addr_depth = 0x800,
                                   sample_clk = soc_clk,
                                   sample_data = soc_reg,
                                   sample_enable = soc_capture_sync)
        mux.add(soc_reg_sampler, 0x2800)

        soc_ddr_0 = ConcatSignal(soc_bus.DQ1_OE, soc_bus.DQS1_O, soc_bus.DQS1_OE, soc_bus.DQ0_I, soc_bus.DM0_I, soc_bus.DQS0_I)
        soc_ddr_1 = ConcatSignal(soc_bus.DQ0_OE, soc_bus.DQS0_O, soc_bus.DQS0_OE, soc_bus.DQ1_I, soc_bus.DM1_I, soc_bus.DQS1_I)

        soc_ddr_sampler_0 = Sampler(addr_depth = 0x800,
                                    sample_clk = soc_clk,
                                    sample_data = soc_ddr_0,
                                    sample_enable = soc_capture_sync)
        mux.add(soc_ddr_sampler_0, 0x3000)

        soc_ddr_sampler_1 = Sampler(addr_depth = 0x800,
                                    sample_clk = soc_clk,
                                    sample_data = soc_ddr_1,
                                    sample_enable = soc_capture_sync)
        mux.add(soc_ddr_sampler_1, 0x3800)

    ####################################################################
    # Random stuff

    if 1:
        pins = ConcatSignal(cclk,
                            i2c_sda, i2c_scl,
                            ext_trig_out, probe_comp,
                            ac_trig, ba7406_hd, ba7406_vd, trig,
                            ref_clk,
                            bank2)
        hc = HybridCounter()
        mux.add(hc, 0, pins)

    # I have a bug somewhere in my Mux, unless I add this the adc
    # sample buffer won't show up in the address range.  I should fix
    # it but I haven't managed to figure out what's wrong yet.
    if 1:
        ram3 = Ram(addr_depth = 1024, data_width = 32)
        mux.add(ram3, 0x8000)

    wb_slave = mux

    # Create the wishbone bus
    wb_bus = wb_slave.create_bus()
    wb_bus.CLK_I = clk_buf
    wb_bus.RST_I = None

    # Create the SPI slave
    spi = SpiSlave()
    spi.addr_width = 32
    spi.data_width = 32

    wb_inst = wb_slave.gen(wb_bus)
    insts.append(wb_inst)

    slave_spi_inst = spi.gen(slave_spi_bus, wb_bus)
    insts.append(slave_spi_inst)

    return insts

def impl():
    from rhea.build.boards import get_board

    brd = get_board('sds7102')
    flow = brd.get_flow(top = top)

    if 1:
        flow.add_files([
            '../../../ip/mig/rtl/mcb_controller/iodrp_controller.v',
            '../../../ip/mig/rtl/mcb_controller/iodrp_mcb_controller.v',
            '../../../ip/mig/rtl/mcb_controller/mcb_raw_wrapper.v',
            '../../../ip/mig/rtl/mcb_controller/mcb_soft_calibration.v',
            '../../../ip/mig/rtl/mcb_controller/mcb_soft_calibration_top.v',
            '../../../ip/mig/rtl/mcb_controller/mcb_ui_top.v',
            ])

    if 0:
        flow.add_files([
            '../../../ip/mig/rtl/example_top.v',
            '../../../ip/mig/rtl/infrastructure.v',

            '../../../ip/mig/rtl/memc_wrapper.v',

            '../../../ip/mig/rtl/memc_tb_top.v',
            '../../../ip/mig/rtl/traffic_gen/afifo.v',
            '../../../ip/mig/rtl/traffic_gen/cmd_gen.v',
            '../../../ip/mig/rtl/traffic_gen/cmd_prbs_gen.v',
            '../../../ip/mig/rtl/traffic_gen/data_prbs_gen.v',
            '../../../ip/mig/rtl/traffic_gen/init_mem_pattern_ctr.v',
            '../../../ip/mig/rtl/traffic_gen/mcb_flow_control.v',
            '../../../ip/mig/rtl/traffic_gen/mcb_traffic_gen.v',
            '../../../ip/mig/rtl/traffic_gen/rd_data_gen.v',
            '../../../ip/mig/rtl/traffic_gen/read_data_path.v',
            '../../../ip/mig/rtl/traffic_gen/read_posted_fifo.v',
            '../../../ip/mig/rtl/traffic_gen/sp6_data_gen.v',
            '../../../ip/mig/rtl/traffic_gen/tg_status.v',
            '../../../ip/mig/rtl/traffic_gen/v6_data_gen.v',
            '../../../ip/mig/rtl/traffic_gen/wr_data_gen.v',
            '../../../ip/mig/rtl/traffic_gen/write_data_path.v',
            ])

    flow.run()
    info = flow.get_utilization()
    pprint(info)

if __name__ == '__main__':
    impl()
