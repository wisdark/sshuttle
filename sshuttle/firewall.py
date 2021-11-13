import errno
import socket
import signal
import sys
import os
import platform
import traceback
import subprocess as ssubprocess

import sshuttle.ssyslog as ssyslog
import sshuttle.helpers as helpers
from sshuttle.helpers import debug1, debug2, Fatal
from sshuttle.methods import get_auto_method, get_method

HOSTSFILE = '/etc/hosts'


def rewrite_etc_hosts(hostmap, port):
    BAKFILE = '%s.sbak' % HOSTSFILE
    APPEND = '# sshuttle-firewall-%d AUTOCREATED' % port
    old_content = ''
    st = None
    try:
        old_content = open(HOSTSFILE).read()
        st = os.stat(HOSTSFILE)
    except IOError as e:
        if e.errno == errno.ENOENT:
            pass
        else:
            raise
    if old_content.strip() and not os.path.exists(BAKFILE):
        os.link(HOSTSFILE, BAKFILE)
    tmpname = "%s.%d.tmp" % (HOSTSFILE, port)
    f = open(tmpname, 'w')
    for line in old_content.rstrip().split('\n'):
        if line.find(APPEND) >= 0:
            continue
        f.write('%s\n' % line)
    for (name, ip) in sorted(hostmap.items()):
        f.write('%-30s %s\n' % ('%s %s' % (ip, name), APPEND))
    f.close()

    if st is not None:
        os.chown(tmpname, st.st_uid, st.st_gid)
        os.chmod(tmpname, st.st_mode)
    else:
        os.chown(tmpname, 0, 0)
        os.chmod(tmpname, 0o600)
    os.rename(tmpname, HOSTSFILE)


def restore_etc_hosts(hostmap, port):
    # Only restore if we added hosts to /etc/hosts previously.
    if len(hostmap) > 0:
        debug2('undoing /etc/hosts changes.')
        rewrite_etc_hosts({}, port)


# Isolate function that needs to be replaced for tests
def setup_daemon():
    if os.getuid() != 0:
        raise Fatal('You must be root (or enable su/sudo) to set the firewall')

    # don't disappear if our controlling terminal or stdout/stderr
    # disappears; we still have to clean up.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # ctrl-c shouldn't be passed along to me.  When the main sshuttle dies,
    # I'll die automatically.
    try:
        os.setsid()
    except OSError:
        raise Fatal("setsid() failed. This may occur if you are using sudo's "
                    "use_pty option. sshuttle does not currently work with "
                    "this option. An imperfect workaround: Run the sshuttle "
                    "command with sudo instead of running it as a regular "
                    "user and entering the sudo password when prompted.")

    # because of limitations of the 'su' command, the *real* stdin/stdout
    # are both attached to stdout initially.  Clone stdout into stdin so we
    # can read from it.
    os.dup2(1, 0)

    return sys.stdin, sys.stdout


# Note that we're sorting in a very particular order:
# we need to go from smaller, more specific, port ranges, to larger,
# less-specific, port ranges. At each level, we order by subnet
# width, from most-specific subnets (largest swidth) to
# least-specific. On ties, excludes come first.
# s:(inet, subnet width, exclude flag, subnet, first port, last port)
def subnet_weight(s):
    return (-s[-1] + (s[-2] or -65535), s[1], s[2])


def flush_systemd_dns_cache():
    # If the user is using systemd-resolve for DNS resolution, it is
    # possible for the request to go through systemd-resolve before we
    # see it...and it may use a cached result instead of sending a
    # request that we can intercept. When sshuttle starts and stops,
    # this means that we should clear the cache!
    #
    # The command to do this was named systemd-resolve, but changed to
    # resolvectl in systemd 239.
    # https://github.com/systemd/systemd/blob/f8eb41003df1a4eab59ff9bec67b2787c9368dbd/NEWS#L3816

    if helpers.which("resolvectl"):
        debug2("Flushing systemd's DNS resolver cache: "
               "resolvectl flush-caches")
        ssubprocess.Popen(["resolvectl", "flush-caches"],
                          stdout=ssubprocess.PIPE, env=helpers.get_env())
    elif helpers.which("systemd-resolve"):
        debug2("Flushing systemd's DNS resolver cache: "
               "systemd-resolve --flush-caches")
        ssubprocess.Popen(["systemd-resolve", "--flush-caches"],
                          stdout=ssubprocess.PIPE, env=helpers.get_env())


# This is some voodoo for setting up the kernel's transparent
# proxying stuff.  If subnets is empty, we just delete our sshuttle rules;
# otherwise we delete it, then make them from scratch.
#
# This code is supposed to clean up after itself by deleting its rules on
# exit.  In case that fails, it's not the end of the world; future runs will
# supersede it in the transproxy list, at least, so the leftover rules
# are hopefully harmless.
def main(method_name, syslog):
    helpers.logprefix = 'fw: '
    stdin, stdout = setup_daemon()
    hostmap = {}
    debug1('Starting firewall with Python version %s'
           % platform.python_version())

    if method_name == "auto":
        method = get_auto_method()
    else:
        method = get_method(method_name)

    if syslog:
        ssyslog.start_syslog()
        ssyslog.stderr_to_syslog()

    if not method.is_supported():
        raise Fatal("The %s method is not supported on this machine. "
                    "Check that the appropriate programs are in your "
                    "PATH." % method_name)

    debug1('ready method name %s.' % method.name)
    stdout.write('READY %s\n' % method.name)
    stdout.flush()

    # we wait until we get some input before creating the rules.  That way,
    # sshuttle can launch us as early as possible (and get sudo password
    # authentication as early in the startup process as possible).
    line = stdin.readline(128)
    if not line:
        return  # parent died; nothing to do

    subnets = []
    if line != 'ROUTES\n':
        raise Fatal('expected ROUTES but got %r' % line)
    while 1:
        line = stdin.readline(128)
        if not line:
            raise Fatal('expected route but got %r' % line)
        elif line.startswith("NSLIST\n"):
            break
        try:
            (family, width, exclude, ip, fport, lport) = \
                    line.strip().split(',', 5)
        except BaseException:
            raise Fatal('expected route or NSLIST but got %r' % line)
        subnets.append((
            int(family),
            int(width),
            bool(int(exclude)),
            ip,
            int(fport),
            int(lport)))
    debug2('Got subnets: %r' % subnets)

    nslist = []
    if line != 'NSLIST\n':
        raise Fatal('expected NSLIST but got %r' % line)
    while 1:
        line = stdin.readline(128)
        if not line:
            raise Fatal('expected nslist but got %r' % line)
        elif line.startswith("PORTS "):
            break
        try:
            (family, ip) = line.strip().split(',', 1)
        except BaseException:
            raise Fatal('expected nslist or PORTS but got %r' % line)
        nslist.append((int(family), ip))
        debug2('Got partial nslist: %r' % nslist)
    debug2('Got nslist: %r' % nslist)

    if not line.startswith('PORTS '):
        raise Fatal('expected PORTS but got %r' % line)
    _, _, ports = line.partition(" ")
    ports = ports.split(",")
    if len(ports) != 4:
        raise Fatal('expected 4 ports but got %d' % len(ports))
    port_v6 = int(ports[0])
    port_v4 = int(ports[1])
    dnsport_v6 = int(ports[2])
    dnsport_v4 = int(ports[3])

    assert(port_v6 >= 0)
    assert(port_v6 <= 65535)
    assert(port_v4 >= 0)
    assert(port_v4 <= 65535)
    assert(dnsport_v6 >= 0)
    assert(dnsport_v6 <= 65535)
    assert(dnsport_v4 >= 0)
    assert(dnsport_v4 <= 65535)

    debug2('Got ports: %d,%d,%d,%d'
           % (port_v6, port_v4, dnsport_v6, dnsport_v4))

    line = stdin.readline(128)
    if not line:
        raise Fatal('expected GO but got %r' % line)
    elif not line.startswith("GO "):
        raise Fatal('expected GO but got %r' % line)

    _, _, args = line.partition(" ")
    udp, user, tmark = args.strip().split(" ", 2)
    udp = bool(int(udp))
    if user == '-':
        user = None
    debug2('Got udp: %r, user: %r, tmark: %s' %
           (udp, user, tmark))

    subnets_v6 = [i for i in subnets if i[0] == socket.AF_INET6]
    nslist_v6 = [i for i in nslist if i[0] == socket.AF_INET6]
    subnets_v4 = [i for i in subnets if i[0] == socket.AF_INET]
    nslist_v4 = [i for i in nslist if i[0] == socket.AF_INET]

    try:
        debug1('setting up.')

        if subnets_v6 or nslist_v6:
            debug2('setting up IPv6.')
            method.setup_firewall(
                port_v6, dnsport_v6, nslist_v6,
                socket.AF_INET6, subnets_v6, udp,
                user, tmark)

        if subnets_v4 or nslist_v4:
            debug2('setting up IPv4.')
            method.setup_firewall(
                port_v4, dnsport_v4, nslist_v4,
                socket.AF_INET, subnets_v4, udp,
                user, tmark)

        flush_systemd_dns_cache()
        stdout.write('STARTED\n')

        try:
            stdout.flush()
        except IOError:
            # the parent process died for some reason; he's surely been loud
            # enough, so no reason to report another error
            return

        # Now we wait until EOF or any other kind of exception.  We need
        # to stay running so that we don't need a *second* password
        # authentication at shutdown time - that cleanup is important!
        while 1:
            line = stdin.readline(128)
            if line.startswith('HOST '):
                (name, ip) = line[5:].strip().split(',', 1)
                hostmap[name] = ip
                debug2('setting up /etc/hosts.')
                rewrite_etc_hosts(hostmap, port_v6 or port_v4)
            elif line:
                if not method.firewall_command(line):
                    raise Fatal('expected command, got %r' % line)
            else:
                break
    finally:
        try:
            debug1('undoing changes.')
        except BaseException:
            debug2('An error occurred, ignoring it.')

        try:
            if subnets_v6 or nslist_v6:
                debug2('undoing IPv6 changes.')
                method.restore_firewall(port_v6, socket.AF_INET6, udp, user)
        except BaseException:
            try:
                debug1("Error trying to undo IPv6 firewall.")
                debug1(traceback.format_exc())
            except BaseException:
                debug2('An error occurred, ignoring it.')

        try:
            if subnets_v4 or nslist_v4:
                debug2('undoing IPv4 changes.')
                method.restore_firewall(port_v4, socket.AF_INET, udp, user)
        except BaseException:
            try:
                debug1("Error trying to undo IPv4 firewall.")
                debug1(traceback.format_exc())
            except BaseException:
                debug2('An error occurred, ignoring it.')

        try:
            # debug2() message printed in restore_etc_hosts() function.
            restore_etc_hosts(hostmap, port_v6 or port_v4)
        except BaseException:
            try:
                debug1("Error trying to undo /etc/hosts changes.")
                debug1(traceback.format_exc())
            except BaseException:
                debug2('An error occurred, ignoring it.')

        try:
            flush_systemd_dns_cache()
        except BaseException:
            try:
                debug1("Error trying to flush systemd dns cache.")
                debug1(traceback.format_exc())
            except BaseException:
                debug2("An error occurred, ignoring it.")
