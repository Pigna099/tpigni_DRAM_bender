"""""
Code for monitoring and controlling temperature in the Maxwell FT200 controller using the Modbus RS485 protocol
@author: Lois Orosa
"""""
import minimalmodbus as mm, serial, time
import time
import os
import sys
import pathlib
import argparse
from argparse import RawTextHelpFormatter

class FT200:
    """
        Controls and monitors the Maxwell FT200 tempoerature controller
    """

    def __init__(self, _connectAndConfig=True, _deviceID = 0, _autoSetAndWait_algorithm = 0, _baud=9600, _ft200_addr=1, verbose=False):
        self.baud   = _baud
        self.write_reg = RW_registers.SV #5 # Write register
        self.read_reg  = R_registers.PV  #0 # Read register
        self.ft200_addr = _ft200_addr # Address of the device (by default is 1)
        self.autoSetAndWait_algorithm = _autoSetAndWait_algorithm 
        self.time_sleep = 1 # 1 second wait per loop iteration
        self.verbose = verbose
        self.deviceID = _deviceID
    
        if _connectAndConfig:
            try:
                self.connect()
                self.configRampSoakModes()
                self.configIntegral()
            except:
                raise

        
    # (Re)Connects to the USB device
    def connect(self):
        try:
            self.devices = self.findDevices() # Get the USB-RS485 device
            if self.verbose:
                print("USB-RS485 Device(s): "+str(self.devices))
            self.instrument = mm.Instrument(self.devices[self.deviceID], 3 , debug = False)
            self.instrument.serial.baudrate = self.baud
            self.instrument.address = self.ft200_addr
            self.instrument.serial.timeout  = 1
            self.instrument.serial.parity   = serial.PARITY_NONE
            self.instrument.mode = mm.MODE_RTU
            # print("Device "+self.device+": conected")
        except:
            raise

    def configRampSoakModes(self):
        # Setting the RAMP and Soak mode
        try:
            self.set_reg(RW_registers.PMD, 2) # Set the RAMP and Soak Mode (value = 2)
            self.set_reg(RW_registers.AMRS, 0) # PID Mode (value = 0)
            self.set_reg(RW_registers.T1, 0) # T1 soak time
            self.set_reg(RW_registers.T1UN, 0) # Timer unit (0 = seconds)
            self.set_reg(RW_registers.TSP, 20) # Timer kick-in temperature
            self.set_reg(RW_registers.PEND, 1)
            self.set_reg(RW_registers.RAMP, 20)
        except:
            raise

    def configIntegral(self):
        # Reset the integral control
        # Disbling and Enabling the integral solves the problem of getting stuck at a temperature that is not the target
        try:
            self.set_reg(RW_registers.I1, 0) # Disable the Integral
            self.set_reg(RW_registers.D1, 0) # Derivative off
            self.set_reg(RW_registers.RST1, 10) # 
            self.set_reg(RW_registers.I1, 20) # Enable the integral again
            self.set_reg(RW_registers.OPL1, 1) # 
        except:
            raise

    def autoTuning(self):
        '''
            Feature of the FT200 to auto-calibrate
            This should be done once in a while
            Auto-tuning should be done at room temperature
        '''
        print("[AUTO-TUNING]")
        print("[Setting temperature to 50 Celsius]")
        tolerance = 70 # 7C of tolerance 
        self.autoSetAndWait(500, tolerance) # 50C
        print("[STARTING AUTO-TUNING]")
        self.set_reg(RW_registers.AT, 1) # Auto-Tuning: better to do it at room temperature
        while self.get_reg(RW_registers.AT):
            time.sleep(self.time_sleep) # WAit for a while
        print("[AUTO-TUNING FINISHED]")


    def findDevices(self):
        '''
            Find the USB-RS485 device
            This function might raise two exceptions:
                -> NameError: There is more than 1 USB-RS485 device, or other USB device has a similar name as the USB-RS485 device
                -> IOError:   Device not found. 
        '''
        #DEVICENAME = "1a86_USB" OLD LINE
        DEVICENAME = "1a86_USB_Serial"
        name = []
        #while True:
        path = pathlib.Path(__file__).parent.absolute()
        f = os.popen(str(path)+"/usb_dev_path.sh")
        for device in f:
            if DEVICENAME in device:
                #if name == '':
                name.append(device.split()[0])
                #print("[device] "+str(name))
                #else:
                #    name = device.split()[0]
                    #print("[device] "+str(name))
                    #raise NameError("There are at least two USB devices whose name contains "+str(DEVICENAME))
        if name == []:
            raise IOError("No device found.")
        #print("[device] "+str(name))
        return name

    
    def set_reg(self, reg, val):
        '''
        Set register to a specific value (only RW registers)
        '''
        while True:
            try:
                self.instrument.write_register(reg, val, functioncode=6, signed=True)
                return 0
            except:
                print("[WARNING] Failed to write to resgister "+str(reg)+" (val="+str(val)+"). Trying again in "+str(self.time_sleep)+" seconds")
                time.sleep(self.time_sleep) # WAit for a while


    def get_reg(self, reg):
        '''
        Read value of a register (R and RW registers)
        '''
        while True:
            try:
                data = self.instrument.read_register(reg, functioncode=3)
                return data
            except:
                print("[WARNING] Failed to read from register "+str(reg)+". Trying again in "+str(self.time_sleep)+" seconds")
                time.sleep(self.time_sleep) # WAit for a while

    # Returns the register value (same as above). But when the value cannot be read correctly, it tries again for 100 times at most without warning the user.
    def get_reg_silent(self, reg):
        NUM_MAX_TRIES=100
        SLEEP_TIME=0.3 # seconds
        cur_try = 0
        while True:
            try:
                data = self.instrument.read_register(reg, functioncode=3)
                return data
            except:
                cur_try += 1

                if cur_try >= NUM_MAX_TRIES:
                    print("ERROR: Tried "+str(NUM_MAX_TRIES)+" times and could not read the register: "+str(reg))

                time.sleep(SLEEP_TIME)

    def readTemp(self):
        '''
            Read Temperature from FT200
            The output value has 1 decimal, and it is multiplied by 10
            E.g., data= 102 represents a temperature of 10.2C 
        '''
        return self.get_reg(R_registers.PV)

    def readTempSilent(self):
        return self.get_reg_silent(R_registers.PV)
   

    def isTemp(self, temperature, tolerance = 2):
        '''
            Check if the temperature reached a value
            The input value has 1 decimal, and it is multiplied by 10
            E.g., data= 102 represents a temperature of 10.2C 
        '''
        data = self.get_reg(R_registers.PV)
        if data >= (temperature-tolerance) and data <= (temperature+tolerance):
            return True
        else:
            return False


    def setTemp(self, value):
        '''
            Set Temperature FT200
            The value should be a integer, and it should be the temperature value with 1 decimal multiplied by 10
            E.g., value= 102 represents a temperature of 10.2C 
        '''
        self.set_reg(RW_registers.SV, value)
        # Trick to avoid that the temperature stalls at a value that is not the target
        self.set_reg(RW_registers.I1, 0) # Disable the Integral
        self.set_reg(RW_registers.I1, 20) # Enable the Integral
        return 0

    # Changes the target temperature. The value for 'new_target_temp' is celsius degrees (e.g., 45 for 45C)
    def changeTargetTemp(self, new_target_temp):
        self.set_reg(RW_registers.SV, new_target_temp*10) # we divide by 10 because this is the format that the PID controller uses to represent temperature in C (e.g., C*10) 

    def autoSetAndWait(self, temperature, tolerance = 2, debug = False):
        '''
            Set the temperature and wait for the temperature to be stable
            The value should be a integer, and it should be the temperature value with 1 decimal multiplied by 10
            E.g., value= 102 represents a temperature of 10.2C 
        '''
        if self.autoSetAndWait_algorithm == 0:
            self.autoSetAndWait_0(temperature, tolerance, debug)
        elif self.autoSetAndWait_algorithm == 1:
            self.autoSetAndWait_1(temperature, tolerance, debug)
        else:
            print("[ERROR] autoSetAndWait algorithm "+str(self.autoSetAndWait_algorithm)+" is not implemented.")
            sys.exit(-1)
         
    
    def autoSetAndWait_0(self, target, tolerance = 2, debug = False):
        '''
        Simplest Set and Wait.
        Test: 11 min (starting at 75C)
        '''
        RAMP = 20 # default RAMP
        stability = 10
        stable_count = stability
        self.set_reg(RW_registers.RAMP, RAMP) # Set the RAMP
        self.setTemp(target) # Set the temperature
        while True:
            current = self.readTemp()
            # Adjust the RAMP
            toTarget = abs(current-target)
            print("[Temp.][RAMP="+str(self.get_reg(RW_registers.RAMP))+"] "+str(current/10.0) + ", target: "+str(target/10.0)+", counter: "+str(stable_count))
            if current <= target + tolerance and current >= target - tolerance:
                stable_count -= 1
            else:
                stable_count = stability 
            if stable_count == 0:
                break
            time.sleep(self.time_sleep) # WAit for a while

    def autoSetAndWait_1(self, target, tolerance = 2, debug = False):
        '''
        Simple Set and Wait.
        We modify the RAMp depending on the distance to the target
        Test: 13 min (starting at 75C)
        '''
        RAMP_MAX = 9999
        RAMP_MID = 4000
        RAMP_LOW = 100
        stability = 10
        stable_count = stability
        self.setTemp(target) # Set the temperature
        RAMP_PREV = -1 # It must be a value different from RAMP_MAX/MID/LOW
        while True:
            current = self.readTemp()
            # Adjust the RAMP
            toTarget = abs(current-target)
            if toTarget > 100: # larger than 10 degrees
                RAMP = RAMP_MAX
            elif toTarget > 40: # larger than 4 degrees
                RAMP = RAMP_MID
            else: # larger than 2 degrees
                RAMP = RAMP_LOW
            if RAMP != RAMP_PREV:
                self.set_reg(RW_registers.RAMP, RAMP) # Set the RAMP acordingly
            print("[Temp.][RAMP="+str(self.get_reg(RW_registers.RAMP))+"] "+str(current/10.0) + ", target: "+str(target/10.0)+", counter: "+str(stable_count))
            RAMP_PREV = RAMP # update
            if current <= target + tolerance and current >= target - tolerance:
                stable_count -= 1
            else:
                stable_count = stability 
            if stable_count == 0:
                break
            time.sleep(self.time_sleep) # WAit for a while


class R_registers:
    '''
    READ ONLY Registers
    '''
    PV   = 0 # Current Temperature values
    MV1  = 1 #
    MV2  = 2
    MVFB = 3
    OLED = 4
    UAD  = 17
    TD   = 27

class RW_registers:
    '''
    READ/WRITE Registers
    '''
    SV   = 5
    SF00 = 6
    SV1  = 7
    SV2  = 8
    SV3  = 9
    SV4  = 10
    SF01 = 11
    AT   = 12
    AMRS = 13
    AL1  = 14
    AL2  = 15
    SF02 = 18
    RAMP = 20
    T1   = 21
    SF03 = 22
    SC   = 23
    P1   = 24
    I1   = 25
    D1   = 26
    ATVL = 28
    CYT1 = 29
    HYS1 = 30
    RST1 = 31
    OPL1 = 32 
    OPH1 = 33
    BUF1 = 34
    PKO1 = 35
    OLAP = 36
    GAP2 = 38
    P2   = 39
    I2   = 40
    D2   = 41
    MOR  = 42
    CYT2 = 43
    HYS2 = 44
    RST2 = 45
    OPL2 = 46
    OPH2 = 47
    SF04 = 49
    SFSV = 50
    STME = 51
    SOUT = 52
    SF05 = 53
    LBAT = 54
    LBAB = 55
    HBAT = 56
    HBAB = 57
    SF06 = 58
    _1LR = 59
    _2LR = 60
    SF07 = 62
    LCK  = 63
    SF08 = 64
    INP1 = 65
    DPD  = 66
    UDUS = 67
    LSPL = 68
    USPL = 69
    PVOS = 70
    PVFT = 71
    ANL1 = 72
    ANH1 = 73
    TRSL = 74
    TRSH = 75
    ALD1 = 76
    AH1  = 77
    ALT1 = 78
    ALD2 = 79
    AH2  = 80
    ALT2 = 81
    OUD1 = 85
    BER1 = 86
    RUCY = 88
    PMD  = 91
    TSP  = 92
    PEND = 93
    IDNO = 94
    BAUD = 95
    UCR  = 96
    EXC1 = 97
    A1L1 = 98
    EXC2 = 99
    A1L2 = 100
    KAM  = 103
    KRS  = 104
    KATU = 105
    PWON = 106
    SFST = 107
    TRS  = 108
    PFBK = 109
    RESV = 110
    MONI = 111
    BEAM = 112
    T1UN = 113
    REMS = 114

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Temperature Controller Script', formatter_class=RawTextHelpFormatter)
    parser.add_argument('-t', help="Type of action/test", choices=["set","get","devices","autotuning","monitor","autoSetAndWait","testArray"], default="monitor")
    parser.add_argument('-c', help="Temperature (Celsius)", default=50.0)
    parser.add_argument('-d', help="Target Device ID", default=0)
    args = parser.parse_args()
    ttype = args.t
    ttemperature = float(args.c)
    deviceID = int(args.d)

    ## Example of how to use the FT200 class
    try:
        tc = FT200(True,deviceID)
    except Exception as error:
        print("[ERROR] "+ repr(error))
        sys.exit(-1)

    if ttype == "devices":
        # Print the current temperature
        print("[devices]:")
        for i,device in enumerate(tc.devices):
            print("    ["+str(i)+"] "+str(device))
    
    if ttype == "get":
        # Print the current temperature
        print("[device]["+str(deviceID)+"] "+str(tc.devices[deviceID]))
        print("Current Temperature: " + str(tc.readTemp()/10))
        print("Target Temperature: " + str(tc.get_reg(RW_registers.SV)/10.0))
    
    if ttype == "set":
        # Set the current temperature
        tc.setTemp(ttemperature*10)
        print("[device]["+str(deviceID)+"] "+str(tc.devices[deviceID]))
        print("Current Temperature: " + str(tc.readTemp()/10.0))
        print("Target Temperature: " + str(tc.get_reg(RW_registers.SV)/10.0))

    if ttype == "autotuning":
        # Calibrate the temperature controller
        # Auto-tuning (it might take 1-2 minutes)
        tc.autoTuning()

    if ttype == "monitor":
        # Monitor the content of some ft200 registers
        # Used mainly for monitoring the actual temperature, and for debugging
        while True:
            plotstring  = "Temp: "+str(tc.readTemp()/10.0)+"C "
            plotstring += "[target: "+str(tc.get_reg(RW_registers.SV)/10.0)+"C]"
            plotstring += "[RAMP: "+str(tc.get_reg(RW_registers.RAMP))+"]"
            plotstring += "[PMD: "+str(tc.get_reg(RW_registers.PMD))+"]"
            plotstring += "[AMRS: "+str(tc.get_reg(RW_registers.AMRS))+"]"
            plotstring += "[T1: "+str(tc.get_reg(RW_registers.T1))+"]"
            plotstring += "[I1: "+str(tc.get_reg(RW_registers.I1))+"]"
            plotstring += "[D1: "+str(tc.get_reg(RW_registers.D1))+"]"
            plotstring += "[RST1: "+str(tc.get_reg(RW_registers.RST1))+"]"
            plotstring += "[OPL1: "+str(tc.get_reg(RW_registers.OPL1))+"]"
            print(plotstring)
            time.sleep(1) # WAit for 1 second

    if ttype == "testArray":
        # Small tests that goes through an array of target temperatures
        temperatures = [55, 65, 60, 70, 65, 75] # in C degrees
        start = time.time()
        for target in temperatures:
            tc.autoSetAndWait(target*10)
        end = time.time()
        print("[TEST] Total time: "+str(end-start)+" seconds ("+str((end-start)/60.0)+" min), temperature array: "+str(temperatures))

    if ttype == "autoSetAndWait":
        # Tests the autoSetAndWait function
        # Set and wait for a target temperature
        ini    = tc.readTemp()
        target = 500
        start = time.time()
        tc.autoSetAndWait(target)
        end = time.time()
        print("From "+str(ini/10.0)+"C to "+str(target/10.0)+"C in "+str(end-start)+" seconds")

    
