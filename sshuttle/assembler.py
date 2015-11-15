import sys
import zlib
import imp

z = zlib.decompressobj()
while 1:
    name = sys.stdin.readline().strip()
    if name:
        nbytes = int(sys.stdin.readline())
        if verbosity >= 2:
            sys.stderr.write('server: assembling %r (%d bytes)\n'
                             % (name, nbytes))
        content = z.decompress(sys.stdin.read(nbytes))

        module = imp.new_module(name)
        parent, _, parent_name = name.rpartition(".")
        if parent != "":
            setattr(sys.modules[parent], parent_name, module)

        code = compile(content, name, "exec")
        exec code in module.__dict__
        sys.modules[name] = module
    else:
        break

sys.stderr.flush()
sys.stdout.flush()

import sshuttle.helpers
sshuttle.helpers.verbose = verbosity

from sshuttle.server import main
main()