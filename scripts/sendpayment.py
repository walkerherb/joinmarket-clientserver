#! /usr/bin/env python
from __future__ import absolute_import, print_function

"""
A sample implementation of a single coinjoin script,
adapted from `sendpayment.py` in Joinmarket-Org/joinmarket.
This is designed
to illustrate the main functionality of the new architecture:
this code can be run in a separate environment (but not safely
over the internet, better on one machine) to the joinmarketdaemon.
Moreover, it can run several transactions as specified in a "schedule", like:

[(mixdepth, amount, N, destination),(m,a,N,d),..]

call it like the normal Joinmarket sendpayment, but optionally add
a port for the daemon:

`python sendpayment.py -p 27183 -N 3 -m 1 walletseed amount address`;

Schedule can be read from a file with the -S option, in which case no need to
provide amount, mixdepth, number of counterparties or destination from command line.

The idea is that the "backend" (daemon) will keep its orderbook and stay
connected on the message channel between runs, only shutting down
after all are complete. Joins are sequenced using the wallet-notify function as
previously for Joinmarket.

It should be very easy to extend this further, of course.

More complex applications can extend from Taker and add
more features. This will also allow
easier coding of non-CLI interfaces. A plugin for Electrum is in process
and already working.

Other potential customisations of the Taker object instantiation
include:

external_addr=None implies joining to another mixdepth
in the same wallet.

order_chooser can be set to a different custom function that selects
counterparty offers according to different rules.
"""

import random
import sys
import threading
from optparse import OptionParser
from twisted.internet import reactor
import time

from jmclient import (Taker, load_program_config, get_schedule,
                              JMTakerClientProtocolFactory, start_reactor,
                              validate_address, jm_single,
                              choose_orders, choose_sweep_orders, pick_order,
                              cheapest_order_choose, weighted_order_choose,
                              Wallet, BitcoinCoreWallet, sync_wallet,
                              RegtestBitcoinCoreInterface, estimate_tx_fee)

from jmbase.support import get_log, debug_dump_object

log = get_log()

def main():
    parser = OptionParser(
        usage=
        'usage: %prog [options] [wallet file / fromaccount] [amount] [destaddr]',
        description='Sends a single payment from a given mixing depth of your '
        +
        'wallet to an given address using coinjoin and then switches off. Also sends from bitcoinqt. '
        +
        'Setting amount to zero will do a sweep, where the entire mix depth is emptied')
    parser.add_option(
        '-f',
        '--txfee',
        action='store',
        type='int',
        dest='txfee',
        default=-1,
        help=
        'number of satoshis per participant to use as the initial estimate ' +
        'for the total transaction fee, default=dynamically estimated, note that this is adjusted '
        +
        'based on the estimated fee calculated after tx construction, based on '
        + 'policy set in joinmarket.cfg.')
    parser.add_option(
        '-w',
        '--wait-time',
        action='store',
        type='float',
        dest='waittime',
        help='wait time in seconds to allow orders to arrive, default=15',
        default=15)
    parser.add_option(
        '-N',
        '--makercount',
        action='store',
        type='int',
        dest='makercount',
        help='how many makers to coinjoin with, default random from 4 to 6',
        default=random.randint(4, 6))
    parser.add_option('-p',
                      '--port',
                      type='int',
                      dest='daemonport',
                      help='port on which joinmarketd is running',
                      default='27183')
    parser.add_option('-S',
                      '--schedule-file',
                      type='str',
                      dest='schedule',
                      help='schedule file name',
                      default='')
    parser.add_option(
        '-C',
        '--choose-cheapest',
        action='store_true',
        dest='choosecheapest',
        default=False,
        help=
        'override weightened offers picking and choose cheapest. this might reduce anonymity.')
    parser.add_option(
        '-P',
        '--pick-orders',
        action='store_true',
        dest='pickorders',
        default=False,
        help=
        'manually pick which orders to take. doesn\'t work while sweeping.')
    parser.add_option('-m',
                      '--mixdepth',
                      action='store',
                      type='int',
                      dest='mixdepth',
                      help='mixing depth to spend from, default=0',
                      default=0)
    parser.add_option('-a',
                      '--amtmixdepths',
                      action='store',
                      type='int',
                      dest='amtmixdepths',
                      help='number of mixdepths in wallet, default 5',
                      default=5)
    parser.add_option('-g',
                      '--gap-limit',
                      type="int",
                      action='store',
                      dest='gaplimit',
                      help='gap limit for wallet, default=6',
                      default=6)
    parser.add_option('--yes',
                      action='store_true',
                      dest='answeryes',
                      default=False,
                      help='answer yes to everything')
    parser.add_option(
        '--rpcwallet',
        action='store_true',
        dest='userpcwallet',
        default=False,
        help=('Use the Bitcoin Core wallet through json rpc, instead '
              'of the internal joinmarket wallet. Requires '
              'blockchain_source=json-rpc'))
    parser.add_option('--fast',
                      action='store_true',
                      dest='fastsync',
                      default=False,
                      help=('choose to do fast wallet sync, only for Core and '
                            'only for previously synced wallet'))

    (options, args) = parser.parse_args()
    load_program_config()

    if options.schedule == '' and len(args) < 3:
        parser.error('Needs a wallet, amount and destination address')
        sys.exit(0)

    #without schedule file option, use the arguments to create a schedule
    #of a single transaction
    sweeping = False
    if options.schedule == '':
        amount = int(args[1])
        if amount == 0:
            sweeping = True
        destaddr = args[2]
        mixdepth = options.mixdepth
        addr_valid, errormsg = validate_address(destaddr)
        if not addr_valid:
            print('ERROR: Address invalid. ' + errormsg)
            return
        schedule = [(options.mixdepth, amount, options.makercount, destaddr)]
    else:
        result, schedule = get_schedule(options.schedule)
        if not result:
            log.info("Failed to load schedule file, quitting. Check the syntax.")
            log.info("Error was: " + str(schedule))
            sys.exit(0)
        mixdepth = 0
        for s in schedule:
            if s[1] == 0:
                sweeping = True
            #only used for checking the maximum mixdepth required
            mixdepth = max([mixdepth, s[0]])

    wallet_name = args[0]

    #for testing, TODO remove
    jm_single().maker_timeout_sec = 5

    chooseOrdersFunc = None
    if options.pickorders:
        chooseOrdersFunc = pick_order
        if sweeping:
            print('WARNING: You may have to pick offers multiple times')
            print('WARNING: due to manual offer picking while sweeping')
    elif options.choosecheapest:
        chooseOrdersFunc = cheapest_order_choose
    else:  # choose randomly (weighted)
        chooseOrdersFunc = weighted_order_choose

    # Dynamically estimate a realistic fee if it currently is the default value.
    # At this point we do not know even the number of our own inputs, so
    # we guess conservatively with 2 inputs and 2 outputs each
    if options.txfee == -1:
        options.txfee = max(options.txfee, estimate_tx_fee(2, 2))
        log.debug("Estimated miner/tx fee for each cj participant: " + str(
            options.txfee))
    assert (options.txfee >= 0)

    log.debug('starting sendpayment')

    if not options.userpcwallet:
        max_mix_depth = max([mixdepth, options.amtmixdepths])
        wallet = Wallet(wallet_name, max_mix_depth, options.gaplimit)
    else:
        wallet = BitcoinCoreWallet(fromaccount=wallet_name)
    sync_wallet(wallet, fast=options.fastsync)

    def taker_finished(res, fromtx=False):
        if fromtx:
            if res:
                sync_wallet(wallet, fast=options.fastsync)
                clientfactory.getClient().clientStart()
            else:
                #a transaction failed; just stop
                reactor.stop()
        else:
            if not res:
                log.info("Did not complete successfully, shutting down")
            else:
                log.info("All transactions completed correctly")
            reactor.stop()

    if isinstance(jm_single().bc_interface, RegtestBitcoinCoreInterface):
        #to allow testing of confirm/unconfirm callback for multiple txs
        jm_single().bc_interface.tick_forward_chain_interval = 10
    taker = Taker(wallet,
                  schedule,
                  options.answeryes,
                  order_chooser=chooseOrdersFunc,
                  callbacks=(None, None, taker_finished))
    clientfactory = JMTakerClientProtocolFactory(taker)
    start_reactor("localhost", options.daemonport, clientfactory)

if __name__ == "__main__":
    main()
    print('done')
