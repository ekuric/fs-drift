# bengland/fedora:28 includes git and any other packages that we use
FROM docker.io/fedora:28
MAINTAINER Ben England <bengland@redhat.com>
RUN dnf install -y python2 git python2-numpy
RUN git clone https://github.com/parallel-fs-utils/fs-drift
RUN ln -sv /fs-drift/fs-drift-remote.py /usr/local/bin
COPY launch.sh /
CMD /launch.sh
