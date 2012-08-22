#!/usr/bin/python
import os
import socket
import subprocess
import time

import Checksum
from tests import BasicTest

"""
Add the tests you want to run here. Don't modify anything outside this function!

You'll need to import the tests here and then create an instance of each one
you want to run. The tests automatically register themselves with the
forwarder, so they will magically be run.
"""
def tests_to_run(forwarder):
    from tests import BasicTest, RandomDropTest
    BasicTest.BasicTest(forwarder, "README")
    RandomDropTest.RandomDropTest(forwarder, "README")

"""
Testing is divided into two pieces: this forwarder and a set of test cases in
the tests directory.

This forwarder literally forwards packets between a sender and a receiver. The
forwarder accepts two files -- a sender and a receiver implementation and a
port to use. Test cases must then be registered with the forwarder. Once test
cases are registered, the forwarder executes each one. Execution involves
starting the specified sender and receiver implementation, and then sending
whatever file the test case specifies, and then calling the test case's
result() method to get a test result back.

The forwarder maintains two queues of packets, the in_queue and the out_queue.
Every packet that arrives is added to the in_queue (after having its
destination re-written appropriately), and every packet that is meant to be
sent is put into the out_queue. The forwarder never moves packets between these
two queues on its own -- that is the responsibility of the test case. Inside
the forwarder and test cases, it's safe to assume all connections start with
sequence number 0: the forwarder rewrites sequence numbers appropriate before
sending packets onward.

The forwarder's main loop (in start()) first checks for any inbound packets. If
a packet is received, the forwarder adds it to the in_queue, then calls the
current test case's handle_packet() method. If no packet is available it checks
whether or not its "tick" interval has expired.  If the tick interval has
expired, we execute a tick event, which calls the test case's handle_tick()
method and then sends over the wire any packets in the out_queue.

Once the sender has terminated, we kill the receiver and call the test case's
result() method, which should do something sensible to determine whether or not
the test case passed.
"""
class Forwarder(object):
    """
    The packet forwarder for testing
    """
    def __init__(self, sender_path, receiver_path, port):
        if not os.path.exists(sender_path):
            raise ValueError("Could not find sender path: %s" % sender_path)
        self.sender_path = sender_path

        if not os.path.exists(receiver_path):
            raise ValueError("Could not find receiver path: %s" % receiver_path)
        self.receiver_path = receiver_path

        # book keeping for tests
        self.tests = {} # test object => input file (so we know what file to feed the sender)
        self.current_test = None
        self.out_queue = []
        self.in_queue = []
        self.test_state = "INIT"
        self.tick_interval = 0.001 # 1ms
        self.last_tick = time.time()
        self.timeout = 600. # seconds

        # network stuff
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.01) # make this a very short timeout, por que no?
        self.sock.bind(('', self.port))

        self.receiver_port = self.port + 1
        self.sender_addr = None
        self.receiver_addr = None

    def _tick(self):
        """
        Every tick, we call the tick handler for the current test, then we
        flush the out_queue.
        """
        self.current_test.handle_tick(self.tick_interval)
        for p in self.out_queue:
            self._send(p)
        self.out_queue = []

    def _send(self, packet):
        """ Send a packet. """
        packet.update_packet(seqno=packet.seqno + self.start_seqno_base)
        self.sock.sendto(packet.full_packet, packet.address)

    def register_test(self, testcase, input_file):
        assert isinstance(testcase, BasicTest.BasicTest)
        self.tests[testcase] = input_file

    def execute_tests(self):
        for t in self.tests:
            self.current_test = t
            self.start()

    def handle_receive(self, message, address):
        """
        Every time we receive a new packet, this is called. We first check if
        this is the first packet we've seen -- if so, we need to learn the
        starting sequence number.

        Otherwise, we add every packet we get to the in_queue.
        """

        # Handle new senders.
        # We need to learn the sender and receiver ports, then learn the
        # initial sequence number so we can just assume every sequence number
        # starts from zero in the test.
        if self.test_state == "NEW":
            if not address[1] == self.receiver_port:
                start_packet = Packet(message, (None, None), 0)
                if not start_packet.bogon:
                    self.start_seqno_base = start_packet.seqno
                    self.sender_addr = address
                    self.test_state = "READY"

        if address == self.receiver_addr:
            p = Packet(message, self.sender_addr, self.start_seqno_base)
        elif address == self.sender_addr:
            p = Packet(message, self.receiver_addr, self.start_seqno_base)
        else:
            raise ValueError("Packet from unknown source: %s" % str(address))
        self.in_queue.append(p)
        self.current_test.handle_packet()

    def start(self):
        self.test_state = "NEW"
        self.sender_addr = None
        self.receiver_addr = ('127.0.0.1', self.receiver_port)
        self.recv_outfile = "127.0.0.1.%d" % self.port

        receiver = subprocess.Popen(["python", self.receiver_path,
                                     "-p", str(self.receiver_port)])
        sender = subprocess.Popen(["python", self.sender_path,
                                   "-f", self.tests[self.current_test],
                                   "-p", str(self.port)])
        try:
            start_time = time.time()
            while sender.poll() is None:
                try:
                    message, address = self.sock.recvfrom(4096)
                    self.handle_receive(message, address)
                except socket.timeout:
                    pass
                if time.time() - self.last_tick > self.tick_interval:
                    self.last_tick = time.time()
                    self._tick()
                if time.time() - start_time > self.timeout:
                    raise Exception("Test timed out!")
            self._tick()
        except (KeyboardInterrupt, SystemExit):
            exit()
        finally:
            if sender.poll() is None:
                sender.kill()
            receiver.kill()

        self.current_test.result(self.recv_outfile)

class Packet():
    def __init__(self, packet, address, start_seqno_base):
        self.full_packet = packet
        self.address = address # where the packet is destined to

        # this is for making sure we have 0-indexed seq numbers throughout the
        # test.
        self.start_seqno_base = start_seqno_base
        try:
            pieces = packet.split('|')
            self.msg_type, self.seqno = pieces[0:2] # first two elements always treated as msg type and seqno
            self.checksum = pieces[-1] # last is always treated as checksum
            self.data = '|'.join(pieces[2:-1]) # everything in between is considered data
            self.seqno = int(self.seqno) - self.start_seqno_base
            assert(self.msg_type in ["start","data","ack","end"])
            int(self.checksum)
            self.bogon = False
        except Exception as e:
            # If a packet is invalid, this is set to true. We don't do anything
            # special otherwise, and it's passed along like every other packet.
            # However, since invalid packets may have undefined contents, it's
            # recommended to just pass these along and do no further processing
            # on them.
            self.bogon = True

    def update_packet(self, msg_type=None, seqno=None, data=None, full_packet=None, update_checksum=True):
        if not self.bogon:
            if msg_type == None:
                msg_type = self.msg_type
            if seqno == None:
                seqno = self.seqno
            if data == None:
                data = self.data

            if data and len(data) > 0:
                body = "%s|%d|%s|" % (msg_type,seqno,data)
            else:
                body = "%s|%d|" % (msg_type, seqno)
            if update_checksum:
                checksum = Checksum.generate_checksum(body)
            else:
                checksum = self.checksum
            self.msg_type = msg_type
            self.seqno = seqno
            self.data = data
            self.checksum = checksum
            if full_packet:
                self.full_packet = full_packet
            else:
                self.full_packet = "%s%s" % (body,checksum)

    def __repr__(self):
        return "%s|%s|...|%s" % (self.msg_type, self.seqno, self.checksum)

if __name__ == "__main__":
    # Don't modify anything below this line!
    import argparse
    parser = argparse.ArgumentParser(description="Forwarder/Testharness for BEARS-TP")
    parser.add_argument("-p", "--port", help="Base port value (default: 33123)", default=33123)
    parser.add_argument("-s", "--sender", help="path to Sender implementation (default: Sender.py)", default="Sender.py")
    parser.add_argument("-r", "--receiver", help="path to Receiver implementation (default: Receiver.py)", default="Receiver.py")
    args = parser.parse_args()

    f = Forwarder(args.sender, args.receiver, args.port)
    tests_to_run(f)
    f.execute_tests()
