"""
Python3 Class to:
  - Read data from a pipe and publish it to the Google Cloud IoT Core via 
    MQTT messages.  The C brain is writing the data to the pipe.

  - Subscribe for MQTT config messages which contain commands for the device
    to execute.  The config messages are published by the backend for this
    device.  The commands are written as binary structs to a pip that the 
    C brain reads.

JWT (json web tokens) are used for secure device authentication based on RSA
public/private keys. 

After connecting, this process:
 - reads data from a pipe (written by the C brain)
 - publishes data to a common (to all devices) MQTT topic.
 - subscribes for config messages for only this device specific MQTT topic.
 - writes commands to a pipe (read by the C brain)

rbaynes 2018-04-10
"""


import datetime, os, ssl, time, logging, json, sys, traceback
import jwt
import paho.mqtt.client as mqtt

from app.models import IoTConfigModel


#------------------------------------------------------------------------------
# some constants for parsing received commands 
COMMANDS    = 'commands'
MESSAGEID   = 'messageId'
CMD         = 'command'
ARG0        = 'arg0'
ARG1        = 'arg1'
CMD_RUN     = 'runtreatment' 
CMD_STOP    = 'stoptreatment'
CMD_LOAD    = 'loadrecipeintovariable'
CMD_ADD     = 'addvariabletotreatment' 
CMD_EXIT    = 'exittreatments'
CMD_STATUS  = 'status'
CMD_NOOP    = 'noop'
CMD_RESET   = 'reset'


#------------------------------------------------------------------------------
class IoTPubSub:
    """ Manages IoT communications to the Google cloud backend MQTT service """

    # Initialize logging
    extra = {"console_name":"IoT", "file_name": "IoT"}
    logger = logging.getLogger( 'iot' )
    logger = logging.LoggerAdapter( logger, extra )


    # Class member vars
    lastConfigVersion = 0   # The last config message version we have seen.
    deviceId = None         # Our device ID.
    mqtt_topic = None       # PubSub topic we publish to.
    jwt_iat = 0
    jwt_exp_mins = 0
    mqtt_client = None
    logNumericLevel = logging.ERROR # default
    encryptionAlgorithm = 'RS256' # for JWT (RSA 256 bit)
    args = None             # Class configuration


    #--------------------------------------------------------------------------
    def __init__( self ):
        """ Class constructor """
        self.get_env_vars() # get our settings from env. vars.
        self.deviceId = self.args.device_id 

        # read our IoT config settings from the DB (if they exist).
        try:
            c = IoTConfigModel.objects.latest()
            self.lastConfigVersion = c.last_config_version 
        except:
            # or create a DB entry since none exists.
            IoTConfigModel.objects.create( 
                    last_config_version = self.lastConfigVersion )

        # validate our deviceId
        if None == self.deviceId or 0 == len( self.deviceId ):
            self.logger.error( 'Invalid or missing DEVICE_ID env. var.' )
            exit( 1 )
        self.logger.debug( 'Using device_id={}'.format( self.deviceId ))

        # the MQTT events topic we publish messages to
        self.mqtt_topic = '/devices/{}/events'.format( self.deviceId )
        self.logger.debug( 'mqtt_topic={}'.format( self.mqtt_topic ))

        # create a (renewable) client with tokens that will timeout
        try:
            self.jwt_iat = datetime.datetime.utcnow()
            self.jwt_exp_mins = self.args.jwt_expires_minutes
            self.mqtt_client = getMQTTclient( self,
                self.args.project_id, self.args.cloud_region, 
                self.args.registry_id, self.deviceId,
                self.args.private_key_file, self.encryptionAlgorithm,
                self.args.ca_certs, self.args.mqtt_bridge_hostname,
                self.args.mqtt_bridge_port ) 
        except( Exception ) as e:
            self.logger.critical( "Exception creating class:", e )


    #--------------------------------------------------------------------------
    def publishEnvVar( self, varName, values, messageType = 'EnvVar' ):
        """ Publish a single environment variable. """
        try:
            message_obj = {}
            message_obj['messageType'] = messageType
#debugrob: remove experiment and treatment from the system - later
            message_obj['exp'] = self.args.experiment
            message_obj['treat'] = self.args.treatment
            message_obj['var'] = varName
            message_obj['values'] = values

            message_json = json.dumps( message_obj ) # dict obj to JSON string

            # Publish the message to the MQTT topic. qos=1 means at least once
            # delivery. Cloud IoT Core also supports qos=0 for at most once
            # delivery.
            self.mqtt_client.publish( self.mqtt_topic, message_json, qos=1 )

            self.logger.info('publishEnvVar: sent \'{}\' to {}'.format(
                    message_json, self.mqtt_topic))
            return True

        except Exception as e:
            self.logger.critical( "publishEnvVar: Exception: {}".format( e ))
            return False


    #--------------------------------------------------------------------------
    def publishCommandReply( self, commandName, valuesJsonString ):
        """ Publish a reply to a command that was received and 
            successfully processed as an environment variable.
        """
        try:
            if None == commandName or 0 == len( commandName ):
                self.logger.error( "publishCommandReply: missing commandName" )
                return False
    
            if None == valuesJsonString or 0 == len( valuesJsonString ):
                self.logger.error( 
                        "publishCommandReply: missing valuesJsonString" )
                return False
    
            # publish the command reply as an env. var.
            self.publishEnvVar( varName = commandName,      
                                values = valuesJsonString,
                                messageType = 'CommandReply' )
            return True

        except Exception as e:
            self.logger.critical( "publishCommandReply: Exception: %s" % e)
            return False


    #--------------------------------------------------------------------------
    def process_network_events( self ):
        """ Call this function repeatedly from a thread proc or event loop
            to allow processing of IoT messages. 
        """
        try:
            # let the mqtt client process any data it has received or 
            # needs to publish
            self.mqtt_client.loop()

            seconds_since_issue = \
                (datetime.datetime.utcnow() - self.jwt_iat).seconds

            # refresh the JWT if it is about to expire
            if seconds_since_issue > 60 * self.jwt_exp_mins:
                self.logger.debug( 'Refreshing token after {}s'.format(
                        seconds_since_issue ))
                self.jwt_iat = datetime.datetime.utcnow()

                # renew our client with the new token
                self.mqtt_client = getMQTTclient( self,
                    self.args.project_id, self.args.cloud_region, 
                    self.args.registry_id, self.deviceId,
                    self.args.private_key_file, self.encryptionAlgorithm,
                    self.args.ca_certs, self.args.mqtt_bridge_hostname,
                    self.args.mqtt_bridge_port ) 
        except( Exception ) as e:
            self.logger.critical( "Exception processing network events:", e )


    #--------------------------------------------------------------------------
    def save_last_config_version( self, lastConfigVersion ):
        """ Save the last version of a config message (commands) we received.
        """
        try:
            self.lastConfigVersion = lastConfigVersion
            c = IoTConfigModel.objects.latest()
            c.last_config_version = lastConfigVersion 
            c.save()
        except:
            IoTConfigModel.objects.create( 
                last_config_version = lastConfigVersion )


    ####################################################################
    # Private internal classes / methods below here.  Don't call them. #
    ####################################################################

    #--------------------------------------------------------------------------
    # private
    class IoTArgs:
        """ Class arguments with defaults. """
        project_id = None
        registry_id = None
        device_id = None
        private_key_file = None
        cloud_region = None
        ca_certs = None
        mqtt_bridge_hostname = 'mqtt.googleapis.com'
        mqtt_bridge_port = 8883 # clould also be 443
        jwt_expires_minutes = 20
        experiment = 'Exp'
        treatment = 'Treat'


    #--------------------------------------------------------------------------
    # private
    def get_env_vars( self ):
        """
        Get our IoT settings from environment variables and defaults.
        Set our self.logger level.
        Return an IoTArgs.
        """
        try:
            self.args = self.IoTArgs()

            self.args.project_id = os.environ.get('GCLOUD_PROJECT')
            if None == self.args.project_id:
                self.logger.critical('iot_pubsub: validate_env_var: '
                    'Missing GCLOUD_PROJECT environment variable.')
                exit(1)

            self.args.cloud_region = os.environ.get('GCLOUD_REGION')
            if None == self.args.cloud_region:
                self.logger.critical('iot_pubsub: validate_env_var: '
                    'Missing GCLOUD_REGION environment variable.')
                exit(1)

            self.args.registry_id = os.environ.get('GCLOUD_DEV_REG')
            if None == self.args.registry_id:
                self.logger.critical('iot_pubsub: validate_env_var: '
                    'Missing GCLOUD_DEV_REG environment variable.')
                exit(1)

            self.args.device_id = os.environ.get('DEVICE_ID')
            if None == self.args.device_id:
                self.logger.critical('iot_pubsub: validate_env_var: '
                    'Missing DEVICE_ID environment variable.')
                exit(1)

            self.args.private_key_file = os.environ.get('IOT_PRIVATE_KEY')
            if None == self.args.private_key_file:
                self.logger.critical('iot_pubsub: validate_env_var: '
                    'Missing IOT_PRIVATE_KEY environment variable.')
                exit(1)

            self.args.ca_certs = os.environ.get('CA_CERTS')
            if None == self.args.ca_certs:
                self.logger.critical('iot_pubsub: validate_env_var: '
                    'Missing CA_CERTS environment variable.')
                exit(1)

        except Exception as e:
            self.logger.critical('iot_pubsub: validate_env_var: {}'.format(e))
            exit(1)

        return self.args


    #--------------------------------------------------------------------------
    # private
    def parseCommand( self, d, messageId ):
        """ Parse the single command message.
            Returns True or False.
        """
        try:
            # validate keys
            if not validDictKey( d, CMD ):
                self.logger.error( 'Message is missing %s key.' % CMD )
                return False
            if not validDictKey( d, ARG0 ):
                self.logger.error( 'Message is missing %s key.' % ARG0 )
                return False
            if not validDictKey( d, ARG1 ):
                self.logger.error( 'Message is missing %s key.' % ARG1 )
                return False

            # validate the command
            commands = [CMD_RUN, CMD_STOP, CMD_LOAD, CMD_ADD, CMD_EXIT, 
                    CMD_STATUS, CMD_NOOP, CMD_RESET]
            cmd = d[CMD].lower() # compare string command in lower case
            if cmd not in commands:
                self.logger.error( '%s is not a valid command.' % d[CMD] )
                return False

            self.logger.debug('Received command messageId=%s %s %s %s' % 
                    (messageId, d[CMD], d[ARG0], d[ARG1]))

            # write the binary brain command to the FIFO
            # (the brain will validate the args)
            if cmd == CMD_RUN or \
               cmd == CMD_STOP:
                self.logger.info( 'Command: %s' % cmd )
#debugrob: don't ever send treatment ID as arg0 from UI, not used.
#debugrob: write event to jbrain
                return True

            if cmd == CMD_LOAD:
                # arg0: variable name (depends on hard coded hardware config).
                # arg1: JSON recipe string (about 1.8KB).
                self.logger.info( 'Command: %s variable: %s recipe: %s' % \
                    ( cmd, d[ARG0], d[ARG1] ))
#debugrob: write event to jbrain
                return True


            if cmd == CMD_ADD:
                # arg1: variable name (depends on hard coded hardware config).
                self.logger.info( 'Command: %s variable: %s' % \
                    ( cmd, d[ARG1] ))
#debugrob: don't ever send treatment ID as arg0 from UI, not used.
#debugrob: how to write event to jbrain?
                return True


            if cmd == CMD_EXIT or \
            cmd == CMD_STATUS or \
            cmd == CMD_NOOP or \
            cmd == CMD_RESET:
                self.logger.info( 'Command: %s' % cmd )
#debugrob: how to write event to jbrain?
                return True

        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            self.logger.critical( "Exception in parseCommand(): %s" % e)
            traceback.print_tb( exc_traceback, file=sys.stdout )
            return False


    #--------------------------------------------------------------------------
    # private
    def parseConfigMessage( self, d ):
        """ Parse the config messages we receive.
            Arg 'd': dict created from the data received with the 
            config MQTT message.
        """
        try:
            if not validDictKey( d, COMMANDS ):
                self.logger.error( 'Message is missing %s key.' % COMMANDS )
                return
    
            if not validDictKey( d, MESSAGEID ):
                self.logger.error( 'Message is missing %s key.' % MESSAGEID )
                return 
    
            # unpack an array of commands from the dict
            for cmd in d[ COMMANDS ]:
                self.parseCommand( cmd, d[ MESSAGEID ] )
    
        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            self.logger.critical( "Exception in parseConfigMessage(): %s" % e)
            traceback.print_tb( exc_traceback, file=sys.stdout )



##########################################################
# Private internal methods below here.  Don't call them. #
##########################################################

#--------------------------------------------------------------------------
# private
"""
Creates a JWT (https://jwt.io) to establish an MQTT connection.
Args:
    project_id: The cloud project ID this device belongs to
    private_key_file: A path to a file containing an RSA256 private key.
    algorithm: The encryption algorithm to use: 'RS256'.
Returns:
    An MQTT generated from the given project_id and private key, which
    expires in 20 minutes. After 20 minutes, your client will be
    disconnected, and a new JWT will have to be generated.
Raises:
    ValueError: If the private_key_file does not contain a known key.
"""
def create_jwt( ref_self, project_id, private_key_file, algorithm ):
    token = {
      # The time that the token was issued at
      'iat': datetime.datetime.utcnow(),
      # The time the token expires.
      'exp': datetime.datetime.utcnow() + datetime.timedelta(minutes=60),
      # The audience field should always be set to the GCP project id.
      'aud': project_id
    }

    # Read the private key file.
    with open( private_key_file, 'r') as f:
        private_key = f.read()

    ref_self.logger.debug(
        'Creating JWT using {} from private key file {}'.format(
            algorithm, private_key_file ))

    return jwt.encode( token, private_key, algorithm = algorithm )


#--------------------------------------------------------------------------
# private
def error_str( rc ):
    """ Convert a Paho error to a human readable string.  """
    return '{}: {}'.format( rc, mqtt.error_string( rc ))


#--------------------------------------------------------------------------
# private
def on_connect( unused_client, ref_self, unused_flags, rc ):
    """ Callback for when a device connects.  """
    ref_self.logger.debug('on_connect: {}'.format( mqtt.connack_string( rc )))


#------------------------------------------------------------------------------
# private
def on_disconnect( unused_client, ref_self, rc ):
    """ Paho callback for when a device disconnects.  """
    ref_self.logger.debug('on_disconnect: {}'.format( error_str( rc )))


#------------------------------------------------------------------------------
# private
def on_publish( unused_client, ref_self, unused_mid):
    """Paho callback when a message is sent to the broker."""
    ref_self.logger.debug( 'on_publish' )


#------------------------------------------------------------------------------
# private
def on_message( unused_client, ref_self, message ):
    """Callback when the device receives a message on a subscription."""
    payload = message.payload.decode( 'utf-8' )
    # message is a paho.mqtt.client.MQTTMessage, these are all properties:
    ref_self.logger.debug('Received message:\n  {}\n  topic={}\n  Qos={}\n  '
        'mid={}\n  retain={}'.format(
            payload, message.topic, str( message.qos ), str( message.mid ),
            str( message.retain ) ))

    # make sure there is a payload, it could be the first empty config message
    if 0 == len( payload ):
        ref_self.logger.debug('on_message: empty payload.')
        return

    # convert the payload to a dict and get the last config msg version
    messageVersion = 0 # starts before the first config version # of 1
    try:
        payloadDict = json.loads( payload )
        if 'lastConfigVersion' in payloadDict:
            messageVersion = int( payloadDict['lastConfigVersion'] )
    except Exception as e:
        ref_self.logger.debug(
           'on_message: Exception parsing payload: {}'.format(e))
        return

    # The broker will keep sending config messages everytime we connect.
    # So compare this message (if a config message) to the last config
    # version we have seen.
    if messageVersion > ref_self.lastConfigVersion:
        ref_self.save_last_config_version( messageVersion )

        # parse the config message to get the commands in it
        # (and write them to the command pipe)
        ref_self.parseConfigMessage( payloadDict )
    else:
        ref_self.logger.debug('Ignoring this old config message.\n')


#------------------------------------------------------------------------------
# private
def on_log( unused_client, ref_self, level, buf ):
    ref_self.logger.debug('\'{}\' {}'.format(buf, level))


#------------------------------------------------------------------------------
# private
def on_subscribe( unused_client, ref_self, mid, granted_qos ):
    ref_self.logger.debug('on_subscribe')


#------------------------------------------------------------------------------
# private
def getMQTTclient( ref_self,
        project_id, cloud_region, registry_id, device_id, private_key_file,
        algorithm, ca_certs, mqtt_bridge_hostname, mqtt_bridge_port ):
    """
    Create our MQTT client. The client_id is a unique string that identifies
    this device. For Google Cloud IoT Core, it must be in the format below.
    """

    # projects/openag-v1/locations/us-central1/registries/device-registry/devices/my-python-device
    client_id=('projects/{}/locations/{}/registries/{}/devices/{}'.format(
        project_id, cloud_region, registry_id, device_id ))
    ref_self.logger.debug('client_id={}'.format( client_id ))

    # The userdata parameter is a reference to our IoTPubSub instance, so 
    # callbacks can access the object.
    client = mqtt.Client( client_id=client_id, userdata=ref_self )

    # With Google Cloud IoT Core, the username field is ignored, and the
    # password field is used to transmit a JWT to authorize the device.
    client.username_pw_set( username = 'unused',
                            password = create_jwt( ref_self, 
                                project_id, private_key_file, algorithm ))

    # Enable SSL/TLS support.
    client.tls_set( ca_certs=ca_certs, tls_version=ssl.PROTOCOL_TLSv1_2 )

    # Register message callbacks. https://eclipse.org/paho/clients/python/docs/
    # describes additional callbacks that Paho supports. 
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    #client.on_publish = on_publish         # only for debugging
    #client.on_subscribe = on_subscribe     # only for debugging
    #client.on_log = on_log                 # only for debugging

    # Connect to the Google MQTT bridge.
    client.connect( mqtt_bridge_hostname, mqtt_bridge_port )

    # This is the topic that the device will receive COMMANDS on:
    mqtt_config_topic = '/devices/{}/config'.format( device_id )
    ref_self.logger.debug('mqtt_config_topic={}'.format( mqtt_config_topic ))

    # Subscribe to the config topic.
    client.subscribe( mqtt_config_topic, qos=1 )

    # Turn on paho debugging manually if you need it for development.
    # client.enable_self.logger() 

    return client


#------------------------------------------------------------------------------
# private
def validDictKey( d, key ):
    """ utility function to check if a key is in a dict. """
    if key in d:
        return True
    else:
        return False





#debugrob: delete later
"""
            # safely try to convert the data we read into a dict
            try:
                # make a py dict from JSON string
                data = json.loads( data_bytes ) 
            except( Exception ) as e:
                self.logger.error('iot_pubsub: read invalid data from pipe.')
                continue

            # verify which json object this is
            if 'command_reply' in data:  # does the dict contain this key ?
                publishCommandReply( client, mqtt_topic, 
                    args.experiment, args.treatment, data )
                continue 

            # at this point, we must have an env-var json object?
            if 't_id' not in data:  
                self.logger.critical('iot_pubsub: received unknown data.')
                continue 

#            values = ''
#            if 'values' in data:
#                values = data['values'] # a complex json style string
#
#            # continue processing the data here
#            self.logger.debug( 'iot_pubsub: %s[%s], %s[%s], values:%s' % 
#                    ( TREAT[ data['t_id'] ]['name'], data['t_id'], 
#                      tvars[ data['v_id'] ]['name'], data['v_id'], 
#                      values ))

#            publishEnvVar( varName, values )

        except( Exception ) as e:
            self.logger.critical( "iot_pubsub: Exception reading data:", e )
"""
