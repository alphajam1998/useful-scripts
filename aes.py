#! /usr/bin/env python
#
# Portable file encrypt/decrypt using AES.
# Tested on:
#  - Python 2.7
#  - Python 3.4
#  - Python 3.5
#
# (c) 2012 Sudhi Herle <sw at herle.net>
# License: GPLv2
#
# Notes:
#  - Does not seek the input or output streams. Thus, this script can
#    be used in a traditional pipe e.g.,
#      tar cf - somedir otherdir | gzip -9 | PW=foo aes.py -k PW -o backup.tar.gz.e encrypt
#  - Handles in-place encryption/decryption - i.e., if same file name is used as
#    the output file, the program does the "right thing"
#  - AES in CTR mode; HMAC-SHA256 for integrity protection
#  - Input is chunked into BUF_SIZE blocks and each chunk is individually HMAC'd
#  - chunk-size & HMAC written as header of ciphertext chunk
#  - Encrypt-then-hash
#  - Uses PBKDF2 to derive two keys (one for hmac and one for cipher)
#  - Single random salt used for KDF
#  - IV for AES-CTR is derived from salt (first 16 bytes of SHA512 of salt)
#  - User supplied passphrase is hashed with SHA512 before sending to KDF.
#  - A password verifier calculated as SHA256 of KDF output; this is used to
#    verify correct password at decryption time.
#  - Verifier, salts, algo choice, KDF params are written as header (struct.pack)
#  - Important information is captured in the header of the encrypted stream.
#    Thus, as long as header format stays same, future versions of the program
#    can still decrypt files encrypted with older version of script.
#
#  - Things that are parametrized are in ALL CAPS at the beginning of the file.
#
# Limitations:
#  - We only support AES-256, SHA-256 and PBKDF2 algorithms
#  - HMAC verification is NOT done before decrypting the data. This is
#    by design to enable stream mode processing (via pipes).
#

import os, sys, stat, string
import argparse, random, getpass
import struct
import string

from binascii             import hexlify
from stat                 import S_ISREG, S_ISFIFO, S_ISSOCK, S_IWUSR, S_ISCHR
from struct               import pack, unpack
from os.path              import dirname, abspath, normpath
from Crypto.Cipher        import AES, Blowfish
from Crypto.Hash          import SHA256, SHA512, HMAC
from Crypto.Util          import Counter
from Crypto.Protocol.KDF  import PBKDF2

PY3K    = sys.version_info >= (3, 0)
CHARSET = list('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')

# Program version
# Increment with every change.
VERSION = '%(prog)s - 0.9'


# -- Parameters that are tunable --

SALTLEN    = 32
KEYLEN     = 32     # AES-256
BUF_SIZE   = 65536  # I/O Bufsize

KEY_ROUNDS = 10000  # PBKDF2 rounds.
CIPHER     = AES
HASH       = SHA256

BLKSIZE    = CIPHER.block_size
HASHSIZE   = HASH.digest_size


# Fixed identifiers for now
AES_id    = 0x0
SHA256_id = 0x0
PBKDF2_id = 0x0



# Fixed length header:
#    ENCALGO, HASHALGO, KDFALGO, KEYLEN, SALTLEN, KEY_ROUNDS, BUF_SIZE
F_HDRFMT   = "> B B B x H H I I"
F_HDRSZ    = struct.calcsize(F_HDRFMT)

# --- --- ---
def main():
    global VERSION, BUF_SIZE

    ap = argparse.ArgumentParser(description="""Portable encryption/decryption of file.
            Encryption uses AES-256 in CTR mode. Encrypted data is authenticated with
            HMAC-SHA-256 (Encrypt-then-MAC). The user supplied passphrase is used to
            derive encryption and HMAC keys using the PBKDF2 function.

            If both input and output file names are identical, then the script
            assumes in-place transformation and uses temporary files for the
            operation.
            """)

    ap.add_argument("-o", "--output", type=str, default=None, metavar='F',
                    help="write output to file 'F' [STDOUT]")
    ap.add_argument("-k", "--env-pass", type=str, default=None, metavar='E',
                    help="read password from environment variable 'E' []")
    ap.add_argument("-B", "--io-buf-size", type=int, default=BUF_SIZE, metavar='N',
                    dest="bufsize",
                    help="Do I/O in chunks of 'N' bytes [%(default)d]")
    ap.add_argument('-V', "--version", action='version', version=VERSION)


    ap.add_argument("op", choices=['encrypt', 'decrypt', 'test'],
                    help="operation to perform")
    ap.add_argument("infile", nargs='?', help="input file to encrypt|decrypt|test [STDIN]")

    args = ap.parse_args()

    if args.bufsize <= 0:
        die("Bufsize %d must be a positive number", args.bufsize)


    BUF_SIZE = args.bufsize

    # Don't ask for password twice if we are decrypting.
    pwd = get_pass(args.env_pass, tries=2 if args.op == "encrypt" else 1)

    if args.op == "test":
        infd        = openif(args.infile, sys.stdin,  'rb')
        outfd       = nullwriter()
        cf          = cipher.decbegin(infd, pwd)
        cf.dec(outfd)
        infd.close()
        return

    #print "PASS=%s ENV=%s" % (pwd, args.env_pass)

    # Verify for in place operation
    inplace = False
    if samefile(args.infile, args.output):
        inplace     = True
        args.infile = abspath(normpath(args.infile))
        args.output = args.infile + randchars(8)

    infd  = openif(args.infile, sys.stdin,  'rb')
    outfd = openif(args.output, sys.stdout, 'wb')

    if args.op == "encrypt":
        cf = cipher('AES', 'SHA256', 'PBKDF2', KEYLEN, SALTLEN, KEY_ROUNDS)
        cf.encbegin(pwd, outfd)
        cf.enc(infd)
    else:
        cf = cipher.decbegin(infd, pwd)
        cf.dec(outfd)

    outfd.close()
    infd.close()

    if inplace:
        tmp = args.infile + randchars(8)

        # Save the orig file temporarily
        try:
            os.rename(args.infile, tmp)
        except Exception as ex:
            os.unlink(args.output)
            die("Operation failed; Unable to create temporary restore point\n\t%s", str(ex))

        try:
            os.rename(args.output, args.infile)
        except Exception as ex:
            os.rename(tmp, args.infile)
            os.unlink(args.output)
            die("Operation failed; unable to rename transformed file back to original\n\t%s", str(ex))
    else:
            os.unlink(tmp)



class cipher:
    """Abstraction of an encrypt/decrypt operation

    XXX We only support AES-256+SHA256+PBKDF2
    """

    def __init__(self, encalgo, hashalgo, kdfalgo, keylen, saltlen, rounds, bufsize=BUF_SIZE):
        #warn("enc=|%s| hash=|%s| kdf=|%s|, keylen=%d, saltlen=%d rounds=%d",
        #        encalgo, hashalgo, kdfalgo, keylen, saltlen, rounds)
        assert encalgo  == 'AES',    ("Encryption algorithm %s is unsupported" % encalgo)
        assert hashalgo == 'SHA256', ("Hash algorithm %s is unsupported" % hashalgo)
        assert kdfalgo  == 'PBKDF2', ("KDF algorithm %s is unsupported" % kdfalgo)
        assert bufsize   > 0,         "Bufsize can't be zero"

        self.kdf     = PBKDF2
        self.cipher  = AES
        self.shash   = SHA256
        self.keylen  = keylen
        self.saltlen = saltlen
        self.rounds  = rounds
        self.bufsize = bufsize
        self.infd    = None
        self.outfd   = None


    def derivekeys(self, pw, salt):
        """Derive the keys needed and return a verifier"""
        blksize = self.cipher.block_size

        # Derive two keys worth of key-material: one for Enc and one for HMAC
        kx  = self.kdf(pw, salt, self.keylen * 2, self.rounds, kdfprf)
        k1  = kx[:self.keylen]
        k2  = kx[self.keylen:]

        # initial counter is derived from the salt
        iv0 = sha512(salt)[:16]
        iv  = int(hexlify(iv0), 16)

        ctr = Counter.new(8 * blksize, initial_value=iv)

        self.k1  = k1
        self.C   = self.cipher.new(k2, self.cipher.MODE_CTR, counter=ctr)

        return pwverifier(k1, k2)


    def hmac(self, chunkoff):
        """Return new HMAC instance"""

        z = pack(">Q", chunkoff)
        k = sha256(self.k1 + z)

        return HMAC.new(k, digestmod=self.shash)

    @classmethod
    def decbegin(klass, infd, pw):
        """Initialize decryption by reading from 'infd'.
        """

        fhdr = infd.read(F_HDRSZ) # fixed size header
        if len(fhdr) < F_HDRSZ:
            die("Header incomplete, expected at least %d bytes, saw %d bytes", F_HDRSZ, len(hdr))

        vv  = unpack(F_HDRFMT, fhdr)
        ea  = vv[0]
        ha  = vv[1]
        ka  = vv[2]

        if ea != 0 or ha != 0 or ka != 0:
            die("Unsupported algorithms in the header")

        kl  = vv[3]
        sl  = vv[4]
        rr  = vv[5]
        bs  = vv[6]

        # Sanity check on unpacked header values
        if kl > 128 or sl > 128:
            die("key-length or salt-length too large")

        if bs == 0 or bs > (1024 * 1048576):
            die("Invalid buffer size in header")

        # XXX If we change algorithms -- replace HASHSIZE with correct size
        # obtained from header algorithm
        vf   = "> %ds %ds" % (sl, HASHSIZE)

        sln  = sl + HASHSIZE
        vhdr = infd.read(sln)
        if len(vhdr) < sln:
            die("Header incomplete, expected %d bytes, saw %d bytes" % (sln, len(vhdr)))

        cf  = cipher('AES', 'SHA256', 'PBKDF2', kl, sl, rr, bufsize=bs)

        vv  = unpack(vf, vhdr)
        vx  = cf.derivekeys(pw, vv[0])
        if not equal(vx, vv[1]):
            die("Password mismatch. Aborting!")

        cf.infd = infd

        return cf

    def encbegin(self, pw, outfd):
        """Initialize operation"""
        self.outfd = outfd

        s1  = randbytes(self.saltlen)
        v   = self.derivekeys(pw, s1)

        vf  = "> %ds %ds" % (self.saltlen, HASHSIZE)
        h1  = pack(F_HDRFMT, 0x0, 0x0, 0x0, self.keylen, self.saltlen, self.rounds, self.bufsize)
        h2  = pack(vf, s1, v)

        hdr = h1 + h2
        self.outfd.write(hdr)


    def enc(self, infd):
        """Encrypt by reading the input fd and writing to outfd"""

        n = 0
        outfd = self.outfd
        while True:
            buf = infd.read(self.bufsize)
            if not buf:
                break

            r = len(buf)
            z = pack(">I", r)
            h = self.hmac(n)
            e = self.C.encrypt(buf)
            h.update(z)
            h.update(e)
            hm = z + h.digest()
            #print "off=%d; sz=%d hash=%s" % (n, r, hexlify(hm[4:]))
            outfd.write(hm)
            outfd.write(e)
            n += len(e)
            n += len(hm)


    def dec(self, outfd):
        """Decrypt by reading the input and writing to outfd"""

        # Each chunk we decrypt must have 4 byte plaintext len plus HMAC of the
        # chunk.
        hdrsz = 4 + HASHSIZE
        infd = self.infd
        off = 0

        while True:
            hdr = infd.read(hdrsz)
            if not hdr:
                break

            if len(hdr) != hdrsz:
                die("Incomplete chunk-header near offset %d; exp %d, saw %d bytes", off, hdrsz, len(hdr))

            h    = self.hmac(off)
            sz   = unpack(">I", hdr[:4])[0]
            hm   = hdr[4:]

            if sz == 0 or sz > self.bufsize:
                die("malformed chunk-header near offset %d; expected size %d, saw %d", off, sz, self.bufsize)

            #print "off=%d; sz=%d hash=%s" % (off, sz, hexlify(hm))

            off += hdrsz
            ebuf = infd.read(sz)
            if len(ebuf) != sz:
                die("Incomplete chunk-body near offset %d; exp %d, saw %d bytes", off, sz, len(ebuf))

            h.update(hdr[:4])
            h.update(ebuf)
            d = h.digest()
            if not equal(d, hm):
                die("Bad chunk near offset %d: MAC mismatch", off)

            buf = self.C.decrypt(ebuf)
            outfd.write(buf)
            off += sz



class nullwriter:
    """Writer that throws away all its output"""
    def __init__(self, *args, **kwargs):
        pass

    def write(self, buf):
        return len(buf)

    def close(self):
        return 0

def regfile(a):
    """Return if st_mode info in 'a' points to a file or file like filesystem
    object"""

    return S_ISREG(a) or S_ISFIFO(a) or S_ISSOCK(a) or S_ISCHR(a)

def samefile(a, b):
    """Return True if a and b are the same file and dirname(a) is writable.

    Return False otherwise."""

    if not a: return False
    if not b: return False

    try:
        sta = os.stat(a)
    except Exception as ex:
        die("%s: %s", a, str(ex))

    if not regfile(sta.st_mode):
        die("%s is not a file like entry?", a)

    # It is NOT an error for the output file to NOT exist!
    try:
        stb = os.stat(b)
    except:
        return False

    if not regfile(stb.st_mode):
        die("%s is not a file like entry?", b)

    if sta.st_ino  != stb.st_ino:   return False
    if sta.st_dev  != stb.st_dev:   return False
    if sta.st_rdev != stb.st_rdev:  return False

    # We only check for writability of dirname(a) only if we are certain that a
    # and b are the same file.
    # Now, make sure the parent dir of 'a' is writable.
    adir = dirname(a)
    try:
        std  = os.stat(adir)
    except Exception as ex:
        die("%s: %s", adir, str(ex))

    if S_IWUSR != (S_IWUSR & std.st_mode):
        die("Directory %s is not writable for inplace operation.", adir)


    return True


def openif(nm, fd, mod):
    """Open 'nm' if non-null else, fdopen 'fd'"""

    if nm == '-': nm = None

    return open(nm, mod, BUF_SIZE) if nm else os.fdopen(fd.fileno(), mod)

def seekable(fd):
    """Return true if fd is a seekable file object"""

    try:
        # try to go to end of file
        z = fd.seek(0, 2)

    except:
        return False
    finally:
        fd.seek(0)

    return True

def warn(fmt, *args):
    sf = fmt % args if args else fmt
    if not sf.endswith('\n'):
        sf += '\n'

    sys.stderr.write(sf)
    sys.stderr.flush()

def die(fmt, *args):
    warn(fmt, *args)
    sys.exit(1)


def equal(a,b):
    """Constant time equals for iterables

    For PY3K, bytes objects are already integers. So, no need for ord()
    """

    if len(a) != len(b): return False

    am = a if PY3K else map(ord, a)
    bm = b if PY3K else map(ord, b)

    v = 0
    for x, y in zip(am, bm):
        v |= x ^ y

    return True if v == 0 else False


def randbytes(n):
    """Get a random string of n bytes length"""
    return os.urandom(n)

def randchars(n):
    """Get a random string of n printable chars"""
    global CHARSET

    random.shuffle(CHARSET)
    x = random.sample(CHARSET, n)

    return ''.join(x)

def dump(fmt, *args):
    """Dump one or more variables containing binary data"""
    if args:
        v = ( hexlify(a) for a in args )
        x = fmt % tuple(v)
    else:
        x = fmt

    if not x.endswith('\n'):
        x += '\n'

    sys.stderr.write(x)
    sys.stderr.flush()

def hashbytes(*a):
    """Hash a and return SHA256"""
    h = HASH.new()
    for x in a:
        lb = pack("> I", len(x))
        h.update(lb)
        h.update(x)

    return h.digest()

def sha512(pw):
    """hash the password once; expands shorter passwords"""
    h  = SHA512.new()
    lb = pack("> I", len(pw))
    h.update(lb)
    h.update(pw)
    return h.digest()

def sha256(s):
    h = SHA256.new()
    lb = pack("> I", len(s))
    h.update(lb)
    h.update(s)
    return h.digest()

def get_pass(env=None, tries=1):
    """Read password from console or environment var"""
    global PY3K

    if PY3K:
        pwenc = lambda x: x.encode('utf-8')
        else:
        pwenc = lambda x: x

    if env:
        pw = os.environ.get(env, None)
        if not pw:
            die("Environment var %s is empty?", pw)

        return sha512(pwenc(pw))

    if tries == 1:
        pw = getpass.getpass("Enter password: ", sys.stderr)
        return sha512(pwenc(pw))

    while tries > 0:
        tries -= 1
        pw1    = getpass.getpass("Enter password: ", sys.stderr)
        pw2    = getpass.getpass("Re-enter password: ", sys.stderr)
        if pw1 == pw2:
            return sha512(pwenc(pw1))

        warn("Passwords don't match. Try again..")

    die("Too many mistakes. Bye!")


def kdfprf(p, s):
    """Pseudo-random-function used by PBKDF2. We use HMAC-SHA512."""

    h0 = HMAC.new(p, digestmod=SHA512)
    h0.update(s)
    return h0.digest()


def pwverifier(k1, k2):
    """Generate password verifier from two KDF derived keys"""

    s = HASH.new()
    s.update(k1)
    s.update(k2)
    return s.digest()


main()

# vim: tw=82:notextmode:expandtab:sw=4:ts=4:
