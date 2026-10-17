# FS_PCB_Testing

This is a util created to facilitate the testing of a microcontroller unit. It allows for a user and operator to read the data from the MCU and to send data, which can be used to actuate pins on the MCU but also as a general way of inputing data.

This repo is composed of 2 parts: a C++ class which has a publish-subscriber topology. It interacts with a Python script that does the bridge between the MCU and the data visualization software, Foxglove.

To run this, you need to install all of the required dependences, required to communicate with foxglove and the MCU via the serial port.

```
pip install -r requirements.txt
```

To run the python code:
```
python ./foxglove_bridge.py
```

# How to use

## Outgoing Topics
We need to declare the data topics at the beginning of the loop
```
// ---- Outgoing topics: declare once, publish every loop. Names show up as "/imu.ax" etc. in Foxglove
FoxgloveDebug::Topic& imu = fg.topic("/imu")
                                .addFloat("ax")
                                .addFloat("ay")
                                .addFloat("az");
```
Then, every time we want to send data to Foxglove, we need to publish it with
```
// Publish: one value per declared field, in the declared order.
imu.publish(ax, ay, az);
```


## Incomming Topics
For incomming signals, we need to bind them in case of a slider or subscribe to them, in case of a btn
```
// Incoming signals. Names must match the "signal name" of the control in the UI.
fg.bind("kp", kp);                    // variable kept in sync automatically
fg.bind("target_rpm", targetRpm);
fg.bind("enable", motorEnabled);
fg.subscribe("reset", onReset);       // or run a function

```

## Setup and loop
To setup this, we need to first run:

```
fg.begin(Serial);      // use e.g. Serial1 (after Serial1.begin(921600)) for a hardware UART
```

And for eahc loop, we need:
```
fg.update();  // process signals coming from the UI (callbacks run here)
```