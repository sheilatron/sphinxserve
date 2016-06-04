#!/usr/bin/env python

'''sphinxserve render sphinx docs when detecting file changes.

usage: sphinxserve [-h] {serve,install,uninstall} ...'''

__version__ = '0.8b1'
__author__ = 'Daniel Mizyrycki'

import gevent.monkey
gevent.monkey.patch_all()

import os
import sys
import signal
from os.path import exists

from gevent.event import Event
from gevent import spawn, joinall
from loadconfig import Config
from loadconfig.lib import write_file
import logging as log
from sphinx import build_main
from sphinxserve.lib import capture_streams, fs_event_ctx, Webserver
from textwrap import dedent


conf = '''\
    app:            sphinxserve
    app_socket:     localhost:8888
    app_user:       1000
    docker_image:   mzdaniel/sphinxserve
    extensions:     [rst, rst~, txt, txt~]

    clg:
        prog: $app
        description: |
            $app $version render sphinx docs when detecting file changes and
            serve them by default on localhost:8888. It uses gevent and flask
            for serving the website and watchdog for recursively monitor
            changes on rst and txt files.
        default_cmd: serve
        subparsers:
            serve:
                help: |
                    serve sphinx docs. (For extra help, use:
                        sphinxserve serve --help)
                options: &options
                    debug:
                        short: d
                        action: store_true
                        default: __SUPPRESS__
                    uid:
                        short: u
                        type: int
                        help: |
                            numeric system user id for rendered pages on Docker
                              (use "id" to retrieve it.  def: $app_user)
                        default: $app_user
                    socket:
                        short: s
                        help: |
                            Launch web server if defined  (def: $app_socket)
                        default: $app_socket
                    output:
                        short: o
                        help: |
                            Directory to which output will be written; defaults
                            to './html/'.
                        default: html
                args: &args
                    sphinx_path:
                        help: |
                            path containing sphinx conf.py
                              (def: $PWD)
                        nargs: '?'
                        default: __SUPPRESS__
            install:
                help: |
                    print commands for docker installation on ~/bin/$app
                      Use as:  docker run $docker_image install | bash
                               ~/bin/$app [SPHINX_PATH]
                options: *options
                args: *args
            uninstall:
                help: print commands for uninstalling $app
    checkconfig: |
        import os
        from os.path import abspath
        if not self.sphinx_path:
           self.sphinx_path = os.getcwd()
        self.sphinx_path = abspath(self.sphinx_path)
    '''


class SphinxServer(object):
    '''Coordinate sphinx greenlets.
    manage method render sphinx pages, initialize and give control to serve,
    watch and render greenlets.
    '''
    def __init__(self, c):
        self.c = c
        self.watch_ev = Event()
        self.render_ev = Event()
        self.shutdown = False

    def shutdown(self):
        '''Gently request all workers finish their work and cleanly shut down.
        '''
        self.shutdown = True
        self.web_server.shutdown = True

    def serve(self):
        '''Serve web requests from path as soon docs are ready.
        Reload remote browser when updates are rendered using websockets
        '''
        host, port = self.c.socket.split(':')
        self.web_server = Webserver(
            os.path.join(self.c.sphinx_path, self.c.output),
            host,
            port,
            self.render_ev
        )
        self.web_server.run()

    def watch(self):
        '''Watch sphinx_path signalling render when rst files change
        '''
        with fs_event_ctx(self.c.sphinx_path, self.c.extensions) as fs_ev_iter:
            for event in fs_ev_iter:
                log.debug('filesystem event: {}'.format(event, event.ev_name))
                self.watch_ev.set()
                if self.shutdown:
                    break

    def render(self):
        '''Render and listen for doc changes (watcher events)
        '''
        while True:
            self.watch_ev.wait()  # Wait for docs changes
            self.watch_ev.clear()
            with capture_streams() as streams:
                self.build()
            log.debug(streams.getvalue())
            self.render_ev.set()
            if self.shutdown:
                break

    def build(self):
        '''Render reStructuredText files with sphinx'''
        return build_main(['sphinx-build', self.c.sphinx_path,
            os.path.join(self.c.sphinx_path, self.c.output)])

    def manage(self):
        '''Manage web server, watcher and sphinx docs renderer
        '''
        with capture_streams() as streams:
            ret = self.build()
        if ret != 0:
            sys.exit(streams.getvalue())
        log.debug(streams.getvalue())
        workers = [spawn(self.serve), spawn(self.watch), spawn(self.render)]
        joinall(workers)


class Prog(object):
    '''Execute cli options'''

    def __init__(self, c):
        self.c = c
        self.shutdown = False

    def check_dependencies(self):
        '''Create sphinx conf.py and index.rst if necessary'''
        path = self.c.sphinx_path
        if not exists(path):
            os.makedirs(path)
        if not exists(path + '/index.rst'):
            data = dedent('''\
                Index rst file
                ==============

                This is the main reStructuredText page. It is meant as a
                temporary example, ready to override.''')
            write_file(path + '/index.rst', data)
        if not exists(path + '/conf.py'):
            write_file(path + '/conf.py', "master_doc = 'index'\n")

    def install(self):
        print(self.c.render(dedent('''\
            mkdir -p ~/bin
            cat > ~/bin/$app << EOF
            #!/bin/bash

            SPHINX_PATH=\${1:-\$PWD}
            USERID="$uid"
            SOCKET="$socket"
            APP_PORT=\${SOCKET#*:}

            usage () {
                echo "Usage: $app [-h] [SPHINX_PATH]    (default: \$PWD)"
                exit 1; }

            [ "\$1" == "-h" ] || [ "\$1" == "--help" ] && usage

            docker run -it -u \$USERID -v \$SPHINX_PATH:/host  \
                -p \$APP_PORT:\$APP_PORT $docker_image \
                -s 0.0.0.0:\$APP_PORT /host
            EOF
            chmod 755 ~/bin/$app
            ''')))

    def uninstall(self):
        print(self.c.render('rm -f ~/bin/$app'))

    def serve(self):
        '''Create the SphinxServer with the provided configuration,
        and start it listening to events.

        This method catches SIGINT signals, so it needs to call
        sys.exit(0) after the SphinxServer shuts itself down.
        '''

        def shutdown_handler(signal, frame):
            log.info("Received SIGNINT signal to shut down!")
            self.shutdown = True
            self.sphinx_server.shutdown()

        signal.signal(signal.SIGINT, shutdown_handler)
        self.check_dependencies()
        self.sphinx_server = SphinxServer(self.c).manage()
        log.info("Completed a clean shutdown.")
        sys.exit(0)


def main(args):
    c = Config(conf, args=args, version=__version__)
    if c.debug:
        log.root.setLevel(log.DEBUG)
    # Run command selected from cli (corresponding to conf subparser, and
    # Prog method. Eg: serve)
    c.run(Prog)

if __name__ == "__main__":
    main(sys.argv)
