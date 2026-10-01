#!/usr/bin/env python3
# Copyright (c) 2026, The OTNS Authors.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the
#    names of its contributors may be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# Sleepy Router "purge" scenario -- see openthread/docs/sleepy-router/DESIGN_LOG.md, section 6,
# point 2, and the bug analysis in the chat log dated 2026-09-29.
#
# `Mle::RemoveNeighbor()` (mle_ftd.cpp) has a dedicated cleanup branch for a
# removed Child (`IndirectSender::ClearAllMessagesForSleepyChild`) but no
# equivalent for a removed Router: any message still queued indirectly for a
# Sleepy Router (`Message::IsPendingForRouter()`) at the moment that Router's
# RouterTable entry is invalidated is never dequeued, never freed, and its
# `RouterId` may later be reassigned to a completely different Router --
# risking not just a permanent buffer leak but a misdelivery to whichever new
# Router later reuses that same RouterId.
#
# This test reproduces the leak side of that bug with deterministic timing
# (no manual CLI guessing): the ping and the deletion of the Sleepy Router
# happen in the same simulated instant (no `go()` between them), so the
# queued message is guaranteed to still be pending when the Router
# disappears.

import logging
import unittest

from OTNSTestCase import OTNSTestCase


class SleepyRouterPurgeTest(OTNSTestCase):

    def testMessageDoesNotLeakWhenSleepyRouterDisappears(self):
        ns = self.ns

        # Two Routers merge into one partition. Which of the two ends up
        # Leader is decided by Thread's own partition/weight comparison
        # (not simply "whoever was added first") -- so pick roles from
        # what actually happened instead of assuming node 1 is the Leader.
        ns.add('router', 100, 100)
        ns.add('router', 130, 100)
        ns.go(10)
        self.assertFormPartitions(1)

        leader_id = 1 if ns.get_state(1) == 'leader' else 2
        peer_id = 2 if leader_id == 1 else 1

        # The non-Leader node is forced into genuine Router role instead of
        # falling back to Child, matching the Tappa E/F test procedure --
        # this is the node that will become the Sleepy Router under test.
        ns.node_cmd(peer_id, 'state router')
        ns.go(10)
        self.assertNodeState(peer_id, 'router')

        # The peer becomes a Sleepy Router; give the Leader time to learn
        # its CSL schedule via `Mac::ProcessCsl()` (CSL IE on the peer's
        # outgoing frames) so a ping to it is queued indirectly, not sent
        # directly.
        ns.node_cmd(peer_id, 'sleepyrouter enable')
        ns.go(5)

        baseline = self._free_buffers(leader_id)
        logging.info('Leader (node %d) baseline free buffers: %d', leader_id, baseline)

        # Queue a message for the peer indirectly, then destroy the peer in
        # the same simulated instant -- no `go()` between the two calls, so
        # the message is guaranteed to still be queued (never transmitted)
        # when the peer disappears. `delete()` is a hard removal (no
        # graceful detach announcement), matching a Router that drops off
        # the network without warning.
        ns.ping(leader_id, peer_id)
        ns.delete(peer_id)

        after_delete = self._free_buffers(leader_id)
        logging.info('Leader free buffers right after the peer is deleted: %d', after_delete)
        self.assertLess(after_delete, baseline,
                         'expected the queued ping to hold a message buffer immediately after '
                         'the peer is deleted -- if this fails, the ping was not actually queued '
                         'indirectly and the test setup (not the purge scenario) needs revisiting')

        # Advance past the Leader's neighbor-aging timeout for a Router
        # (`Mle::kMaxNeighborAge` = 100s) plus the Link Request retries and
        # Link Accept timeout that follow it, so `Mle::RemoveNeighbor()`
        # fires for the now-invalid peer entry in the Leader's RouterTable.
        ns.go(180)

        after_timeout = self._free_buffers(leader_id)
        logging.info('Leader free buffers after neighbor-aging timeout: %d', after_timeout)
        self.assertEqual(baseline, after_timeout,
                          "message buffer for the queued ping was never released after the peer was "
                          "removed from the Leader's RouterTable -- orphaned message (purge scenario "
                          'bug, see openthread/docs/sleepy-router/DESIGN_LOG.md section 6 point 2)')

    def _free_buffers(self, nodeid: int) -> int:
        for line in self.ns.node_cmd(nodeid, 'bufferinfo'):
            if line.startswith('free:'):
                return int(line.split(':')[1].strip())
        raise AssertionError("'free:' line not found in bufferinfo output")


if __name__ == '__main__':
    unittest.main()
