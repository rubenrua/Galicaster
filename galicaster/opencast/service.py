# -*- coding:utf-8 -*-
# Galicaster, Multistream Recorder and Player
#
#       galicaster/opencast/ocservice
#
# Copyright (c) 2011, Teltek Video Research <galicaster@teltek.es>
#
# This work is licensed under the Creative Commons Attribution-
# NonCommercial-ShareAlike 3.0 Unported License. To view a copy of 
# this license, visit http://creativecommons.org/licenses/by-nc-sa/3.0/ 
# or send a letter to Creative Commons, 171 Second Street, Suite 300, 
# San Francisco, California, 94105, USA.

import datetime
from os import path
from threading import Timer

from galicaster.utils import ical
from galicaster.mediapackage import mediapackage
from galicaster.opencast.series import get_series

"""
This class manages the timers and its respective signals in order to start and stop scheduled recordings.
It also toggles capture agent status and communicates it to Opencast server.
"""

class OCService(object):

    def __init__(self, repo, occlient, scheduler, conf, disp, logger, recorder):
        """Initializes the scheduler of future recordings.
        The instance of this class is in charge of set all the necessary timers in order to manage scheduled recordings.
        It also manages the update of mediapackages with scheduled recordings and capture agent status.
        Args:
            repo (Repository): the galicaster mediapackage repository. See mediapackage/repository.py.
            scheduler (Scheduler): the galicaster scheduler. See scheduler/scheduler.py.
            conf (Conf): galicaster users and default configuration. See galicaster/core/conf.py.
            disp (Dispatcher): the galicaster event-dispatcher to emit signals. See core/dispatcher.py.
            occlient (MHTTPClient): opencast HTTP client. See utils/mhttpclient.py.
            logger (Logger): the object that prints all the information, warning and error messages. See core/logger.py
            recorder (Recorder)
        Attributes:
            ca_status (str): actual capture agent status.
            old_ca_status (str): old capture agent status.
            conf (Conf): galicaster users and default configuration given as an argument. 
            repo (Repository): the galicaster mediapackage repository given as an argument.
            dispatcher (Dispatcher): the galicaster event-dispatcher to emit signals given by the argument disp.
            client (MHTTPClient): opencast HTTP client given by the argument occlient. 
            logger (Logger): the object that prints all the information, warning and error messages.
            recorder (Recorder)
            t_stop (Timer): timer with the duration of a scheduled recording. 
            start_timers (Dict{str,Timer}): set of timers with the time remaining for all the scheduled recordings that are going to start in less than 30 minutes.
            mp_rec (str): identifier of the mediapackage that is going to be recorded at the scheduled time.
            last_events (List[Events]): list of calendar Events.
            net (bool): True if the connectivity with opencast is up. False otherwise.
        Notes:
            Possible values of ca_status and old_ca_status:
                - idle
                - recording
        """
        self.ca_status = 'idle'
        self.old_ca_status = None

        self.conf       = conf
        self.repo       = repo
        self.scheduler  = scheduler
        self.dispatcher = disp
        self.client     = occlient
        self.logger     = logger
        self.recorder   = recorder

        self.dispatcher.connect('timer-short', self.do_timers_short)
        self.dispatcher.connect('timer-long',  self.do_timers_long)
        self.dispatcher.connect('recorder-started', self.__check_recording_started)
        self.dispatcher.connect('recorder-stopped', self.__check_recording_stopped)
        self.dispatcher.connect("recorder-error", self.on_recorder_error)
        
        self.t_stop = None

        self.start_timers = dict()
        self.mp_rec = None
        self.last_events = self.init_last_events()
        self.net = False
        self.series = []


    def __set_recording_state(self, mp, state):
        self.logger.info("Sending state {} for the scheduled MP {}".format(state, mp))
        try:
            self.client.setrecordingstate(mp.getIdentifier(), state)
        except Exception as exc:
            self.logger.warning('Problems to connect to opencast server: {0}'.format(exc))
            self.dispatcher.emit('opencast-status', False)
            self.net = False

                
    def __check_recording_started(self, element=None, mp_id=None):
        #TODO: Improve the way of checking if it is a scheduled recording
        mp = self.repo.get(mp_id)
        if mp and mp.getOCCaptureAgentProperty('capture.device.names'):
            self.__set_recording_state(mp, 'capturing')
            

    def __check_recording_stopped(self, element=None, mp_id=None):
        #TODO: Improve the way of checking if it is a scheduled recording
        mp = self.repo.get(mp_id)
        if mp and mp.getOCCaptureAgentProperty('capture.device.names'):
            self.__set_recording_state(mp, 'capture_finished')

        
    def init_last_events(self):
        """Initializes the last_events parameter with the events represented in calendar.ical (attach directory).
        Returns:
            List[Events]: list of calendar events. 
        """
        ical_path = self.repo.get_attach_path('calendar.ical')
        if path.isfile(ical_path):
            return ical.get_events_from_file_ical(ical_path)
        else:
            return list()


    def do_timers_short(self, sender):
        """Reviews state of capture agent if connectivity with opencast is up.
        Otherwise initializes opencast's client.
        Args:
            sender (Dispatcher): instance of the class in charge of emitting signals.
        Notes:
            This method is invoked every short beat duration. (10 seconds by default)
        """
        if self.net:
            self.set_state()
        else:
            self.init_client()


    def do_timers_long(self, sender):
        """Calls proccess_ical method in order to process the icalendar received from opencast if connectivity is up.
        Then, calls create_new_timer method with the mediapackages that have scheduled recordings in order to create a new timer if necessary.
        Args:
            sender (Dispatcher): instance of the class in charge of emitting signals.
        Notes:
            This method is invoked every long beat duration (1 minute by default).
        """
        if self.net:
            self.proccess_ical()
            self.dispatcher.emit('ical-processed')
            self.series = get_series()
        for mp in self.repo.get_next_mediapackages():
            self.scheduler.create_new_timer(mp)
        
    
    def init_client(self):
        """Tries to initialize opencast's client and set net's state.
        If it's unable to connecto to opencast server, logger prints ir properly and net is set True.
        """
        self.logger.info('Init opencast client')
        self.old_ca_status = None
        self.dispatcher.emit('opencast-status', None)
        try:
            self.client.welcome()
        except Exception as exc:
            self.logger.warning('Unable to connect to opencast server: {0}'.format(exc))
            self.net = False
            self.dispatcher.emit('opencast-status', False)
        else:
            self.net = True
            # self.dispatcher.emit('opencast-connected')



    def set_state(self):
        """Sets the state of the capture agent.
        Then tries to set opencasts' client state and configuration if there is connectivity.
        If not, logger prints the warning appropriately.
        """

        if self.recorder.is_error():
            self.ca_status = 'unknown' #See AgentState.java
        elif self.recorder.is_recording():
            self.ca_status = 'capturing'
        else:
            self.ca_status = 'idle'
        self.logger.info('Set status %s to server', self.ca_status)        
        
        try:
            self.client.setstate(self.ca_status)
            self.client.setconfiguration(self.conf.get_tracks_in_oc_dict())
            self.net = True
            self.dispatcher.emit('opencast-status', True)
        except Exception as exc:
            self.logger.warning('Problems to connect to opencast server: {0}'.format(exc))
            self.net = False
            self.dispatcher.emit('opencast-status', False)
            return


    def proccess_ical(self):
        """Creates, deletes or updates mediapackages according to scheduled events information given by opencast.
        """
        self.logger.info('Proccess ical')
        try:
            ical_data = self.client.ical()
        except Exception as exc:
            self.logger.warning('Problems to connect to opencast server: {0}'.format(exc))
            self.net = False
            self.dispatcher.emit('opencast-status', False)
            return

        # No data but no error implies that the calendar has not been modified (ETAG)
        if ical_data == None:
            return
        
        try:
            events = ical.get_events_from_string_ical(ical_data)
            delete_events = ical.get_delete_events(self.last_events, events)
            update_events = ical.get_update_events(self.last_events, events)
        except Exception as exc:
            self.logger.error('Error proccessing ical: {0}'.format(exc))
            return

        self.repo.save_attach('calendar.ical', ical_data)
        
        for event in events:
            self.logger.debug('Creating MP with UID {0} from ical'.format(event['UID']))
            ical.create_mp(self.repo, event)
        
        for event in delete_events:
            self.logger.info('Deleting MP with UID {0} from ical'.format(event['UID']))
            mp = self.repo.get(event['UID'])
            if mp.status == mediapackage.SCHEDULED:
                self.repo.delete(mp)
            if self.start_timers.has_key(mp.getIdentifier()):
                self.start_timers[mp.getIdentifier()].cancel()
                del self.start_timers[mp.getIdentifier()]

        for event in update_events:
            self.logger.info('Updating MP with UID {0} from ical'.format(event['UID']))
            mp = self.repo.get(event['UID'])
            if self.start_timers.has_key(mp.getIdentifier()) and mp.status == mediapackage.SCHEDULED:
                self.start_timers[mp.getIdentifier()].cancel()
                del self.start_timers[mp.getIdentifier()]
                self.scheduler.create_new_timer(mp)
                
        self.last_events = events


        
    def on_recorder_error(self, origin=None, error_message=None):
        current_mp_id = self.recorder.current_mediapackage
        if not current_mp_id:
            return
        
        mp = self.repo.get(current_mp_id)
        
        if mp and not mp.manual:
            now_is_recording_time = mp.getDate() < datetime.datetime.utcnow() and mp.getDate() + datetime.timedelta(seconds=(mp.getDuration()/1000)) > datetime.datetime.utcnow()

            if now_is_recording_time:
                self.__set_recording_state(mp, 'capture_error')