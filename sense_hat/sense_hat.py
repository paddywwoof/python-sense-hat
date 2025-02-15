#!/usr/bin/python
import struct
import os
import sys
import math
import time
import numpy as np
import shutil
import glob
import RTIMU  # custom version
import pwd
import array
import fcntl
from PIL import Image  # pillow


class SenseHat(object):

    SENSE_HAT_FB_NAME = 'RPi-Sense FB'
    SENSE_HAT_FB_FBIOGET_GAMMA = 61696
    SENSE_HAT_FB_FBIOSET_GAMMA = 61697
    SENSE_HAT_FB_FBIORESET_GAMMA = 61698
    SENSE_HAT_FB_GAMMA_DEFAULT = 0
    SENSE_HAT_FB_GAMMA_LOW = 1
    SENSE_HAT_FB_GAMMA_USER = 2
    SETTINGS_HOME_PATH = '.config/sense_hat'

    def __init__(
            self,
            imu_settings_file='RTIMULib',
            text_assets='sense_hat_text'
        ):

        self._fb_device = self._get_fb_device()
        if self._fb_device is None:
            raise OSError('Cannot detect %s device' % self.SENSE_HAT_FB_NAME)

        self._rotation = 0

        # Load text assets
        dir_path = os.path.dirname(__file__)
        self._load_text_assets(
            os.path.join(dir_path, '%s.png' % text_assets),
            os.path.join(dir_path, '%s.txt' % text_assets)
        )

        # Load IMU settings and calibration data
        self._imu_settings = self._get_settings_file(imu_settings_file)
        self._imu = RTIMU.RTIMU(self._imu_settings)
        self._imu_init = False  # Will be initialised as and when needed
        self._pressure = RTIMU.RTPressure(self._imu_settings)
        self._pressure_init = False  # Will be initialised as and when needed
        self._humidity = RTIMU.RTHumidity(self._imu_settings)
        self._humidity_init = False  # Will be initialised as and when needed
        self._last_orientation = {'pitch': 0, 'roll': 0, 'yaw': 0}
        raw = {'x': 0, 'y': 0, 'z': 0}
        self._last_compass_raw = raw
        self._last_gyro_raw = raw
        self._last_accel_raw = raw
        self._compass_enabled = False
        self._gyro_enabled = False
        self._accel_enabled = False

    ####
    # Text assets
    ####

    # Text asset files are rotated right through 90 degrees to allow blocks of
    # 40 contiguous pixels to represent one 5 x 8 character. These are stored
    # in a 8 x 640 pixel png image with characters arranged adjacently
    # Consequently we must rotate the pixel map left through 90 degrees to
    # compensate when drawing text

    def _load_text_assets(self, text_image_file, text_file):
        """
        Internal. Builds a character indexed dictionary of pixels used by the
        show_message function below
        """

        #text_pixels = list(self.load_image(text_image_file, False))
        text_pixels = self.load_image(text_image_file, False)
        text_pixels = text_pixels.reshape(-1, 5, 8, 3)
        with open(text_file, 'r') as f:
            loaded_text = f.read()
        self._text_dict = {}
        for i, s in enumerate(loaded_text):
            #start = i * 40
            #end = start + 40
            #char = text_pixels[start:end]
            self._text_dict[s] = text_pixels[i]

    def _trim_whitespace(self, char):  # For loading text assets only
        """
        Internal. Trims white space pixels from the front and back of loaded
        text characters
        

        char is a numpy array shape (5, 8, 3)"""
        
        if char.sum() > 0:
            for i in range(5):
                if char[i].sum() > 0:
                    break
            slice_from = i
            for i in range(4, -1, -1):
                if char[i].sum() > 0:
                    break
            slice_to = i + 1
            return char[slice_from:slice_to]
        return char

    def _get_settings_file(self, imu_settings_file):
        """
        Internal. Logic to check for a system wide RTIMU ini file. This is
        copied to the home folder if one is not already found there.
        """

        ini_file = '%s.ini' % imu_settings_file

        home_dir = pwd.getpwuid(os.getuid())[5]
        home_path = os.path.join(home_dir, self.SETTINGS_HOME_PATH)
        if not os.path.exists(home_path):
            os.makedirs(home_path)

        home_file = os.path.join(home_path, ini_file)
        home_exists = os.path.isfile(home_file)
        system_file = os.path.join('/etc', ini_file)
        system_exists = os.path.isfile(system_file)

        if system_exists and not home_exists:
            shutil.copyfile(system_file, home_file)

        return RTIMU.Settings(os.path.join(home_path, imu_settings_file))  # RTIMU will add .ini internally

    def _get_fb_device(self):
        """
        Internal. Finds the correct frame buffer device for the sense HAT
        and returns its /dev name.
        """

        device = None

        for fb in glob.glob('/sys/class/graphics/fb*'):
            name_file = os.path.join(fb, 'name')
            if os.path.isfile(name_file):
                with open(name_file, 'r') as f:
                    name = f.read()
                if name.strip() == self.SENSE_HAT_FB_NAME:
                    fb_device = fb.replace(os.path.dirname(fb), '/dev')
                    if os.path.exists(fb_device):
                        device = fb_device
                        break

        return device

    ####
    # LED Matrix
    ####

    @property
    def rotation(self):
        return self._rotation

    @rotation.setter
    def rotation(self, r):
        self.set_rotation(r, True)

    def set_rotation(self, r=0, redraw=True):
        """
        Sets the LED matrix rotation for viewing, adjust if the Pi is upside
        down or sideways. 0 is with the Pi HDMI port facing downwards
        """

        if r in [0, 90, 180, 270]:
            if redraw:
                pixel_list = self.get_pixels()
            self._rotation = r
            if redraw:
                self.set_pixels(pixel_list)
        else:
            raise ValueError('Rotation must be 0, 90, 180 or 270 degrees')

    def _xy_rotated(self, x, y):
        """ returns the offset value of the x,y location in the flattened
        form of the array as saved to fb_device stream, adjusting for rotation
        """
        if self._rotation == 0:
            return x + 8 * y
        elif self._rotation == 90:
            return 8 + 8 * x - y
        elif self._rotation == 180:
            return 72 - x - 8 * y
        elif self._rotation == 270:
            return 64 - 8 * x + y
        else:
            raise ValueError('Rotation must be 0, 90, 180 or 270 degrees')

    def _pack_bin(self, pixel_list):
        """
        Internal. Encodes [R,G,B] into 16 bit RGB565
        works on a numpy array (H, W, 3) returns flattened bytes string.
        """
        bits16 = np.zeros(pixel_list.shape[:2], dtype=np.uint16)
        bits16 += np.left_shift(np.bitwise_and(pixel_list[:,:,0], 0xF8), 8)
        bits16 += np.left_shift(np.bitwise_and(pixel_list[:,:,1], 0xFC), 3)
        bits16 += np.right_shift(pixel_list[:,:,2], 3)
        return bits16.tostring()

    def _unpack_bin(self, packed):
        """
        Internal. Decodes 16 bit RGB565 into [R,G,B]
        takes 1D bytes string and produces a 2D numpy array. The calling
        process then needs to reshape that to the correct 3D shape.
        """
        bits16 = np.fromstring(packed, dtype=np.uint16)
        pixel_list = np.zeros((len(bits16), 3), dtype=np.uint16)
        pixel_list[:,0] = np.right_shift(np.bitwise_and(bits16[:], 0xF800), 8)
        pixel_list[:,1] = np.right_shift(np.bitwise_and(bits16[:], 0x07E0), 3)
        pixel_list[:,2] = np.left_shift(np.bitwise_and(bits16[:], 0x001F), 3)
        return pixel_list

    def flip_h(self, redraw=True):
        """
        Flip LED matrix horizontal
        """
        pixel_list = self.get_pixels().reshape(8, 8, 3)
        flipped = np.fliplr(pixel_list)
        if redraw:
            self.set_pixels(flipped)
        return flipped.reshape(64, 3) # for compatibility with flat version

    def flip_v(self, redraw=True):
        """
        Flip LED matrix vertical
        """
        pixel_list = self.get_pixels().reshape(8, 8, 3)
        flipped = np.flipud(pixel_list)
        if redraw:
            self.set_pixels(flipped)
        return flipped.reshape(64, 3) # for compatibility with flat version

    def set_pixels(self, pixel_list):
        """
        Accepts a list containing 64 smaller lists of [R,G,B] pixels or,
        ideally, a numpy array shape (64, 3) or (8, 8, 3) and
        updates the LED matrix. R,G,B elements must be intergers between 0
        and 255
        """
        if not isinstance(pixel_list, np.ndarray):
            pixel_list = np.array(pixel_list, dtype=np.uint16)
        else:
            if pixel_list.dtype != np.uint16:
                pixel_list = pixel_list.astype(np.uint16)
        if pixel_list.shape != (8, 8, 3):
          try:
              pixel_list.shape = (8, 8, 3)
          except:
              raise ValueError('Pixel lists must have 64 elements of 3 values each Red, Green, Blue')
        if pixel_list.max() > 255 or pixel_list.min() < 0: # could use where but is it worth it!
            raise ValueError('A pixel is invalid. Pixel elements must be between 0 and 255')

        with open(self._fb_device, 'wb') as f:
            if self._rotation > 0:
              pixel_list = np.rot90(pixel_list, self._rotation // 90)
            f.write(self._pack_bin(pixel_list))

    def get_pixels(self):
        """
        Returns a list containing 64 smaller lists of [R,G,B] pixels
        representing what is currently displayed on the LED matrix
        """
        with open(self._fb_device, 'rb') as f:
            pixel_list = self._unpack_bin(f.read(128))
            if self._rotation > 0:
              pixel_list.shape = (8, 8, 3)
              pixel_list = np.rot90(pixel_list, (360 - self._rotation) // 90)
        return pixel_list.reshape(64, 3) # existing apps using get_pixels will expect shape (64, 3)

    def set_pixel(self, x, y, *args):
        """
        Updates the single [R,G,B] pixel specified by x and y on the LED matrix
        Top left = 0,0 Bottom right = 7,7

        e.g. ap.set_pixel(x, y, r, g, b)
        or
        pixel = (r, g, b)
        ap.set_pixel(x, y, pixel)
        """

        pixel_error = 'Pixel arguments must be given as (r, g, b) or r, g, b'

        if len(args) == 1:
            pixel = args[0]
            if len(pixel) != 3:
                raise ValueError(pixel_error)
        elif len(args) == 3:
            pixel = args
        else:
            raise ValueError(pixel_error)

        if x > 7 or x < 0:
            raise ValueError('X position must be between 0 and 7')

        if y > 7 or y < 0:
            raise ValueError('Y position must be between 0 and 7')

        for element in pixel:
            if element > 255 or element < 0:
                raise ValueError('Pixel elements must be between 0 and 255')

        with open(self._fb_device, 'wb') as f:
            # Two bytes per pixel in fb memory, 16 bit RGB565
            f.seek(self._xy_rotated(x, y) * 2)
            f.write(self._pack_bin(np.array([[pixel]]))) # need to wrap to 3D

    def get_pixel(self, x, y):
        """
        Returns a list of [R,G,B] representing the pixel specified by x and y
        on the LED matrix. Top left = 0,0 Bottom right = 7,7
        """

        if x > 7 or x < 0:
            raise ValueError('X position must be between 0 and 7')

        if y > 7 or y < 0:
            raise ValueError('Y position must be between 0 and 7')

        pix = None

        with open(self._fb_device, 'rb') as f:
            # Two bytes per pixel in fb memory, 16 bit RGB565
            f.seek(self._xy_rotated(x, y) * 2)
            pix = self._unpack_bin(f.read(2))

        return pix[0]

    def load_image(self, file_path, redraw=True):
        """
        Accepts a path to an 8 x 8 image file and updates the LED matrix with
        the image
        """

        if not os.path.exists(file_path):
            raise IOError('%s not found' % file_path)

        img = Image.open(file_path).convert('RGB')
        sz = img.size[0]
        if sz == img.size[1]: # square image -> scale to 8x8
            img.thumbnail((8, 8), Image.ANTIALIAS) 
        pixel_list = np.array(img)        

        if redraw:
            self.set_pixels(pixel_list)
        return pixel_list.reshape(-1, 3) # in case existing apps use old shape

    def clear(self, *args):
        """
        Clears the LED matrix with a single colour, default is black / off

        e.g. ap.clear()
        or
        ap.clear(r, g, b)
        or
        colour = (r, g, b)
        ap.clear(colour)
        """

        black = (0, 0, 0)  # default

        if len(args) == 0:
            colour = black
        elif len(args) == 1:
            colour = args[0]
        elif len(args) == 3:
            colour = args
        else:
            raise ValueError('Pixel arguments must be given as (r, g, b) or r, g, b')

        self.set_pixels([colour] * 64)

    def _get_char_pixels(self, s):
        """
        Internal. Safeguards the character indexed dictionary for the
        show_message function below
        """

        if len(s) == 1 and s in self._text_dict.keys():
            return self._text_dict[s]
        else:
            return self._text_dict['?']

    def show_message(
            self,
            text_string,
            scroll_speed=.1,
            text_colour=[255, 255, 255],
            back_colour=[0, 0, 0]
        ):
        """
        Scrolls a string of text across the LED matrix using the specified
        speed and colours
        """

        # We must rotate the pixel map left through 90 degrees when drawing
        # text, see _load_text_assets
        previous_rotation = self._rotation
        self._rotation -= 90
        if self._rotation < 0:
            self._rotation = 270
        string_padding = np.zeros((8, 8, 3), np.uint16)
        letter_padding = np.zeros((1, 8, 3), np.uint16)
        # Build pixels from dictionary
        scroll_pixels = np.copy(string_padding)
        for s in text_string:
            scroll_pixels = np.append(scroll_pixels, self._trim_whitespace(self._get_char_pixels(s)), axis=0)
            scroll_pixels = np.append(scroll_pixels, letter_padding, axis=0)
        scroll_pixels = np.append(scroll_pixels, string_padding, axis=0)
        # Recolour pixels as necessary - first get indices of drawn pixels
        f_px = np.where(scroll_pixels[:,:] == np.array([255, 255, 255]))
        scroll_pixels[:,:] = back_colour
        scroll_pixels[f_px[0], f_px[1]] = np.array(text_colour)
        # Then scroll and repeatedly set the pixels
        scroll_length = len(scroll_pixels)
        for i in range(scroll_length - 8):
            self.set_pixels(scroll_pixels[i:i+8])
            time.sleep(scroll_speed)
        self._rotation = previous_rotation

    def show_letter(
            self,
            s,
            text_colour=[255, 255, 255],
            back_colour=[0, 0, 0]
        ):
        """
        Displays a single text character on the LED matrix using the specified
        colours
        """

        if len(s) > 1:
            raise ValueError('Only one character may be passed into this method')
        # We must rotate the pixel map left through 90 degrees when drawing
        # text, see _load_text_assets
        previous_rotation = self._rotation
        self._rotation -= 90
        if self._rotation < 0:
            self._rotation = 270
        pixel_list = np.zeros((8,8,3), np.uint16)
        pixel_list[1:6] = self._get_char_pixels(s)
        # Recolour pixels as necessary - first get indices of drawn pixels
        f_px = np.where(pixel_list[:,:] == np.array([255, 255, 255]))
        pixel_list[:,:] = back_colour
        pixel_list[f_px[0], f_px[1]] = text_colour
        # Finally set pixels
        self.set_pixels(pixel_list)
        self._rotation = previous_rotation

    @property
    def gamma(self):
        buffer = array.array('B', [0]*32) 
        with open(self._fb_device) as f:
            fcntl.ioctl(f, self.SENSE_HAT_FB_FBIOGET_GAMMA, buffer)
        return list(buffer)

    @gamma.setter
    def gamma(self, buffer):
        if len(buffer) is not 32:
            raise ValueError('Gamma array must be of length 32')

        if not all(b <= 31 for b in buffer):
            raise ValueError('Gamma values must be bewteen 0 and 31')

        if not isinstance(buffer, array.array):
            buffer = array.array('B', buffer)

        with open(self._fb_device) as f:
            fcntl.ioctl(f, self.SENSE_HAT_FB_FBIOSET_GAMMA, buffer)

    def gamma_reset(self):
        """
        Resets the LED matrix gamma correction to default
        """

        with open(self._fb_device) as f:
            fcntl.ioctl(f, self.SENSE_HAT_FB_FBIORESET_GAMMA, self.SENSE_HAT_FB_GAMMA_DEFAULT)

    @property
    def low_light(self):
        return self.gamma == [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 10, 10]

    @low_light.setter
    def low_light(self, value):
        with open(self._fb_device) as f:
            cmd = self.SENSE_HAT_FB_GAMMA_LOW if value else self.SENSE_HAT_FB_GAMMA_DEFAULT
            fcntl.ioctl(f, self.SENSE_HAT_FB_FBIORESET_GAMMA, cmd)

    ####
    # Environmental sensors
    ####

    def _init_humidity(self):
        """
        Internal. Initialises the humidity sensor via RTIMU
        """

        if not self._humidity_init:
            self._humidity_init = self._humidity.humidityInit()
            if not self._humidity_init:
                raise OSError('Humidity Init Failed, please run as root / use sudo')

    def _init_pressure(self):
        """
        Internal. Initialises the pressure sensor via RTIMU
        """

        if not self._pressure_init:
            self._pressure_init = self._pressure.pressureInit()
            if not self._pressure_init:
                raise OSError('Pressure Init Failed, please run as root / use sudo')

    def get_humidity(self):
        """
        Returns the percentage of relative humidity
        """

        self._init_humidity()  # Ensure humidity sensor is initialised
        humidity = 0
        data = self._humidity.humidityRead()
        if (data[0]):  # Humidity valid
            humidity = data[1]
        return humidity

    @property
    def humidity(self):
        return self.get_humidity()

    def get_temperature_from_humidity(self):
        """
        Returns the temperature in Celsius from the humidity sensor
        """

        self._init_humidity()  # Ensure humidity sensor is initialised
        temp = 0
        data = self._humidity.humidityRead()
        if (data[2]):  # Temp valid
            temp = data[3]
        return temp

    def get_temperature_from_pressure(self):
        """
        Returns the temperature in Celsius from the pressure sensor
        """

        self._init_pressure()  # Ensure pressure sensor is initialised
        temp = 0
        data = self._pressure.pressureRead()
        if (data[2]):  # Temp valid
            temp = data[3]
        return temp

    def get_temperature(self):
        """
        Returns the temperature in Celsius
        """

        return self.get_temperature_from_humidity()

    @property
    def temp(self):
        return self.get_temperature_from_humidity()

    @property
    def temperature(self):
        return self.get_temperature_from_humidity()

    def get_pressure(self):
        """
        Returns the pressure in Millibars
        """

        self._init_pressure()  # Ensure pressure sensor is initialised
        pressure = 0
        data = self._pressure.pressureRead()
        if (data[0]):  # Pressure valid
            pressure = data[1]
        return pressure

    @property
    def pressure(self):
        return self.get_pressure()

    ####
    # IMU Sensor
    ####

    def _init_imu(self):
        """
        Internal. Initialises the IMU sensor via RTIMU
        """

        if not self._imu_init:
            self._imu_init = self._imu.IMUInit()
            if self._imu_init:
                self._imu_poll_interval = self._imu.IMUGetPollInterval() * 0.001
                # Enable everything on IMU
                self.set_imu_config(True, True, True)
            else:
                raise OSError('IMU Init Failed, please run as root / use sudo')

    def set_imu_config(self, compass_enabled, gyro_enabled, accel_enabled):
        """
        Enables and disables the gyroscope, accelerometer and/or magnetometer
        input to the orientation functions
        """

        # If the consuming code always calls this just before reading the IMU
        # the IMU consistently fails to read. So prevent unnecessary calls to
        # IMU config functions using state variables

        self._init_imu()  # Ensure imu is initialised

        if (not isinstance(compass_enabled, bool)
        or not isinstance(gyro_enabled, bool)
        or not isinstance(accel_enabled, bool)):
            raise TypeError('All set_imu_config parameters must be of boolan type')

        if self._compass_enabled != compass_enabled:
            self._compass_enabled = compass_enabled
            self._imu.setCompassEnable(self._compass_enabled)

        if self._gyro_enabled != gyro_enabled:
            self._gyro_enabled = gyro_enabled
            self._imu.setGyroEnable(self._gyro_enabled)

        if self._accel_enabled != accel_enabled:
            self._accel_enabled = accel_enabled
            self._imu.setAccelEnable(self._accel_enabled)

    def _read_imu(self):
        """
        Internal. Tries to read the IMU sensor three times before giving up
        """

        self._init_imu()  # Ensure imu is initialised

        attempts = 0
        success = False

        while not success and attempts < 3:
            success = self._imu.IMURead()
            attempts += 1
            time.sleep(self._imu_poll_interval)

        return success

    def _get_raw_data(self, is_valid_key, data_key):
        """
        Internal. Returns the specified raw data from the IMU when valid
        """

        result = None

        if self._read_imu():
            data = self._imu.getIMUData()
            if data[is_valid_key]:
                raw = data[data_key]
                result = {
                    'x': raw[0],
                    'y': raw[1],
                    'z': raw[2]
                }

        return result

    def get_orientation_radians(self):
        """
        Returns a dictionary object to represent the current orientation in
        radians using the aircraft principal axes of pitch, roll and yaw
        """

        raw = self._get_raw_data('fusionPoseValid', 'fusionPose')

        if raw is not None:
            raw['roll'] = raw.pop('x')
            raw['pitch'] = raw.pop('y')
            raw['yaw'] = raw.pop('z')
            self._last_orientation = raw

        return self._last_orientation

    @property
    def orientation_radians(self):
        return self.get_orientation_radians()

    def get_orientation_degrees(self):
        """
        Returns a dictionary object to represent the current orientation
        in degrees, 0 to 360, using the aircraft principal axes of
        pitch, roll and yaw
        """

        orientation = self.get_orientation_radians()
        for key, val in orientation.items():
            deg = math.degrees(val)  # Result is -180 to +180
            orientation[key] = deg + 360 if deg < 0 else deg
        return orientation

    def get_orientation(self):
        return self.get_orientation_degrees()

    @property
    def orientation(self):
        return self.get_orientation_degrees()

    def get_compass(self):
        """
        Gets the direction of North from the magnetometer in degrees
        """

        self.set_imu_config(True, False, False)
        orientation = self.get_orientation_degrees()
        if type(orientation) is dict and 'yaw' in orientation.keys():
            return orientation['yaw']
        else:
            return None

    @property
    def compass(self):
        return self.get_compass()

    def get_compass_raw(self):
        """
        Magnetometer x y z raw data in uT (micro teslas)
        """

        raw = self._get_raw_data('compassValid', 'compass')

        if raw is not None:
            self._last_compass_raw = raw

        return self._last_compass_raw

    @property
    def compass_raw(self):
        return self.get_compass_raw()

    def get_gyroscope(self):
        """
        Gets the orientation in degrees from the gyroscope only
        """

        self.set_imu_config(False, True, False)
        return self.get_orientation_degrees()

    @property
    def gyro(self):
        return self.get_gyroscope()

    @property
    def gyroscope(self):
        return self.get_gyroscope()

    def get_gyroscope_raw(self):
        """
        Gyroscope x y z raw data in radians per second
        """

        raw = self._get_raw_data('gyroValid', 'gyro')

        if raw is not None:
            self._last_gyro_raw = raw

        return self._last_gyro_raw

    @property
    def gyro_raw(self):
        return self.get_gyroscope_raw()

    @property
    def gyroscope_raw(self):
        return self.get_gyroscope_raw()

    def get_accelerometer(self):
        """
        Gets the orientation in degrees from the accelerometer only
        """

        self.set_imu_config(False, False, True)
        return self.get_orientation_degrees()

    @property
    def accel(self):
        return self.get_accelerometer()

    @property
    def accelerometer(self):
        return self.get_accelerometer()

    def get_accelerometer_raw(self):
        """
        Accelerometer x y z raw data in Gs
        """

        raw = self._get_raw_data('accelValid', 'accel')

        if raw is not None:
            self._last_accel_raw = raw

        return self._last_accel_raw

    @property
    def accel_raw(self):
        return self.get_accelerometer_raw()

    @property
    def accelerometer_raw(self):
        return self.get_accelerometer_raw()
