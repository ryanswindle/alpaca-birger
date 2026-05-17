from typing import Annotated, Dict

from fastapi import APIRouter, Depends, Form, HTTPException

from exceptions import (
    DriverException,
    InvalidValueException,
    NotConnectedException,
    NotImplementedException,
)
from focuser_device import FocuserDevice
from log import get_logger
from responses import MethodResponse, PropertyResponse, StateValue
from shr import AlpacaGetParams, AlpacaPutParams, alpaca_put_params, to_bool


logger = get_logger()

router = APIRouter(prefix="/api/v1/focuser", tags=["Focuser"])

devices: Dict[int, FocuserDevice] = {}


def set_devices(dev_dict: Dict[int, FocuserDevice]):
    global devices
    devices = dev_dict


def get_device(devnum: int) -> FocuserDevice:
    if devnum not in devices:
        raise HTTPException(
            status_code=400,
            detail=f"Device number {devnum} does not exist.",
        )
    return devices[devnum]


##################################
# High-level device/library info #
##################################
class DeviceMetadata:
    Name = "Birger EF-232"
    Version = "1.0.0"
    Description = "Birger EF-232 ASCOM Alpaca Driver via Canon EF-232 serial library"
    DeviceType = "Focuser"
    Info = "Alpaca Device\nImplements IFocuserV4\nASCOM Initiative"
    InterfaceVersion = 4


def _connected_property(device: FocuserDevice, value, params):
    """Helper for simple properties that require connection."""

    if not device.connected:
        return PropertyResponse.create(
            value=None,
            client_transaction_id=params.client_transaction_id,
            error=NotConnectedException(),
        ).model_dump()
    return PropertyResponse.create(
        value=value,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


#######################################
# ASCOM Methods Common To All Devices #
#######################################
@router.put("/{devnum}/action", summary="")
async def action(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    get_device(devnum)
    return MethodResponse.create(
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("Action"),
    ).model_dump()


@router.put("/{devnum}/commandblind", summary="")
async def commandblind(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    get_device(devnum)
    return MethodResponse.create(
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("CommandBlind"),
    ).model_dump()


@router.put("/{devnum}/commandbool", summary="")
async def commandbool(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    get_device(devnum)
    return MethodResponse.create(
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("CommandBool"),
    ).model_dump()


@router.put("/{devnum}/commandstring", summary="")
async def commandstring(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    get_device(devnum)
    return MethodResponse.create(
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("CommandString"),
    ).model_dump()


@router.put("/{devnum}/connect", summary="")
async def connect(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    device = get_device(devnum)
    try:
        device.connect()
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except Exception as ex:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.Connect failed", ex),
        ).model_dump()


@router.get("/{devnum}/connected", summary="")
async def connected_get(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return PropertyResponse.create(
        value=device.connected,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.put("/{devnum}/connected", summary="")
async def connected_put(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    device = get_device(devnum)
    value = params.get("Connected")
    if value is None:
        raise HTTPException(status_code=400, detail="Missing required parameter 'Connected'")
    conn = to_bool(value)
    try:
        device.connected = conn
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except HTTPException:
        raise
    except Exception as ex:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.Connected failed", ex),
        ).model_dump()


@router.get("/{devnum}/connecting", summary="")
async def connecting_get(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return PropertyResponse.create(
        value=device.connecting,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.get("/{devnum}/description", summary="")
async def description(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=DeviceMetadata.Description,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.get("/{devnum}/devicestate", summary="")
async def devicestate(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    if not device.connected:
        return PropertyResponse.create(
            value=[],
            client_transaction_id=params.client_transaction_id,
            error=NotConnectedException(),
        ).model_dump()
    try:
        val = [
            StateValue(Name="IsMoving", Value=device.is_moving).model_dump(),
            StateValue(Name="Position", Value=device.position).model_dump(),
            StateValue(Name="TimeStamp", Value=device.timestamp).model_dump(),
        ]
        return PropertyResponse.create(
            value=val,
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except Exception as ex:
        return PropertyResponse.create(
            value=[],
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.DeviceState failed", ex),
        ).model_dump()


@router.put("/{devnum}/disconnect", summary="")
async def disconnect(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    device = get_device(devnum)
    try:
        device.disconnect()
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except Exception as ex:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.Disconnect failed", ex),
        ).model_dump()


@router.get("/{devnum}/driverinfo", summary="")
async def driverinfo(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=DeviceMetadata.Info,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.get("/{devnum}/driverversion", summary="")
async def driverversion(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=DeviceMetadata.Version,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.get("/{devnum}/interfaceversion", summary="")
async def interfaceversion(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=DeviceMetadata.InterfaceVersion,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.get("/{devnum}/name", summary="")
async def name(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=DeviceMetadata.Name,
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


@router.get("/{devnum}/supportedactions", summary="")
async def supportedactions(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=[],
        client_transaction_id=params.client_transaction_id,
    ).model_dump()


########################
# IFocuserV4 properties #
########################
@router.get("/{devnum}/absolute", summary="")
async def absolute(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return _connected_property(device, device.absolute, params)


@router.get("/{devnum}/ismoving", summary="")
async def ismoving(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return _connected_property(device, device.is_moving, params)


@router.get("/{devnum}/maxincrement", summary="")
async def maxincrement(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return _connected_property(device, device.max_increment, params)


@router.get("/{devnum}/maxstep", summary="")
async def maxstep(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return _connected_property(device, device.max_step, params)


@router.get("/{devnum}/position", summary="")
async def position_get(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    if not device.connected:
        return PropertyResponse.create(
            value=None,
            client_transaction_id=params.client_transaction_id,
            error=NotConnectedException(),
        ).model_dump()
    try:
        return PropertyResponse.create(
            value=device.position,
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except Exception as ex:
        return PropertyResponse.create(
            value=None,
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.Position failed", ex),
        ).model_dump()


@router.get("/{devnum}/stepsize", summary="")
async def stepsize(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=None,
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("StepSize"),
    ).model_dump()


@router.get("/{devnum}/tempcomp", summary="")
async def tempcomp_get(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return _connected_property(device, device.temp_comp, params)


@router.put("/{devnum}/tempcomp", summary="")
async def tempcomp_put(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    get_device(devnum)
    return MethodResponse.create(
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("TempComp"),
    ).model_dump()


@router.get("/{devnum}/tempcompavailable", summary="")
async def tempcompavailable(devnum: int, params: AlpacaGetParams = Depends()):
    device = get_device(devnum)
    return _connected_property(device, device.temp_comp_available, params)


@router.get("/{devnum}/temperature", summary="")
async def temperature(devnum: int, params: AlpacaGetParams = Depends()):
    get_device(devnum)
    return PropertyResponse.create(
        value=None,
        client_transaction_id=params.client_transaction_id,
        error=NotImplementedException("Temperature"),
    ).model_dump()


@router.put("/{devnum}/halt", summary="")
async def halt(devnum: int, params: AlpacaPutParams = Depends(alpaca_put_params)):
    device = get_device(devnum)
    if not device.connected:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=NotConnectedException(),
        ).model_dump()
    try:
        device.halt()
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except Exception as ex:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.Halt failed", ex),
        ).model_dump()


@router.put("/{devnum}/move", summary="")
async def move(devnum: int, Position: Annotated[str, Form()], params: AlpacaPutParams = Depends(alpaca_put_params)):
    device = get_device(devnum)
    # Validate Position before the connected check so bad values yield 400
    # regardless of connection state, per ConformU.
    try:
        pos = int(Position)
    except ValueError:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=InvalidValueException(f"Position {Position} not a valid integer."),
        ).model_dump()
    if not device.connected:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=NotConnectedException(),
        ).model_dump()
    if pos < 0 or pos > device.max_step:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=InvalidValueException(
                f"Position {Position} out of range (0–{device.max_step})."
            ),
        ).model_dump()

    try:
        device.move(pos)
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
        ).model_dump()
    except Exception as ex:
        return MethodResponse.create(
            client_transaction_id=params.client_transaction_id,
            error=DriverException(0x500, "Focuser.Move failed", ex),
        ).model_dump()
