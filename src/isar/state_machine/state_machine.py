import logging
import queue
from collections import deque
from copy import deepcopy
from typing import Deque, Optional, Tuple

from injector import Injector, inject
from transitions import Machine

from isar.config import config
from isar.models.communication.messages import (
    StartMissionMessages,
    StopMessage,
    StopMissionMessages,
)
from isar.models.communication.queues.queues import Queues
from isar.models.communication.status import Status
from isar.models.mission import Mission
from isar.services.coordinates.transformation import Transformation
from isar.services.service_connections.slimm.slimm_service import SlimmService
from isar.state_machine.states import Cancel, Collect, Idle, Monitor, Off, Send
from models.enums.states import States
from robot_interfaces.robot_scheduler_interface import RobotSchedulerInterface
from robot_interfaces.robot_storage_interface import RobotStorageInterface


class StateMachine(object):
    @inject
    def __init__(
        self,
        queues: Queues,
        scheduler: RobotSchedulerInterface,
        storage: RobotStorageInterface,
        slimm_service: SlimmService,
        transform: Transformation,
        mission_path: str = config.get("mission", "eqrobot_default_mission"),
        sleep_time: float = config.getfloat("mission", "eqrobot_state_machine_sleep"),
        transitions_log_length: int = config.getint(
            "logging", "state_transitions_log_length"
        ),
    ):
        self.logger = logging.getLogger("state_machine")
        self.states = [
            Off(self),
            Idle(self),
            Send(self),
            Monitor(self),
            Collect(self, storage, transform),
            Cancel(self, storage, slimm_service),
        ]
        self.machine = Machine(self, states=self.states, initial="off", queued=True)
        self.queues = queues
        self.scheduler = scheduler

        self.sleep_time = sleep_time
        self.mission_path = mission_path
        self.status: Status = Status(
            mission_status=None,
            mission_in_progress=False,
            current_mission_instance_id=None,
            current_mission_step=None,
            mission_schedule=Mission(mission_steps=[]),
            current_state=States(self.state),  # type: ignore
        )
        self.predefined_mission_id: Optional[int] = None

        self.transitions_log_length = transitions_log_length
        self.transitions_list: Deque[States] = deque([], self.transitions_log_length)

    def begin(self):
        self.log_state_transition(States.Idle)
        self.to_idle()

    def to_next_state(self, next_state):
        self.log_state_transition(next_state)

        if next_state == States.Idle:
            self.to_idle()
        elif next_state == States.Send:
            self.to_send()
        elif next_state == States.Monitor:
            self.to_monitor()
        elif next_state == States.Cancel:
            self.to_cancel()
        elif next_state == States.Collect:
            self.to_collect()
        else:
            self.logger.error("Not valid state direction.")

    def update_status(self):
        self.status.current_state = States(self.state)

    def reset_state_machine(self) -> States:
        self.status.mission_status = None
        self.status.mission_in_progress = False
        self.status.current_mission_instance_id = None
        self.status.current_mission_step = None
        self.status.mission_schedule = Mission(mission_steps=[])

        return States.Idle

    def send_status(self):
        self.queues.mission_status.output.put(deepcopy(self.status))
        self.logger.info(self.status)

    def should_send_status(self) -> bool:
        try:
            send: bool = self.queues.mission_status.input.get(block=False)
            return send
        except queue.Empty:
            return False

    def should_start_mission(self) -> Tuple[bool, Optional[Mission]]:
        try:
            mission: Mission = self.queues.start_mission.input.get(block=False)
        except queue.Empty:
            return False, None

        if not self.status.mission_in_progress and mission is not None:
            return True, mission
        elif self.status.mission_in_progress:
            self.queues.start_mission.output.put(
                deepcopy(StartMissionMessages.mission_in_progress())
            )
            self.logger.info(StartMissionMessages.mission_in_progress())
            return False, None

        return False, None

    def start_mission(self, mission: Mission):
        self.status.mission_in_progress = True
        self.status.mission_schedule = mission
        self.queues.start_mission.output.put(deepcopy(StartMissionMessages.success()))
        self.logger.info(StartMissionMessages.success())

    def should_stop(self) -> bool:
        try:
            stop: bool = self.queues.stop_mission.input.get(block=False)
        except queue.Empty:
            return False

        if stop and self.status.mission_in_progress:
            return True
        elif stop and not self.status.mission_in_progress:
            message: StopMessage = StopMissionMessages.no_active_missions()
            self.queues.stop_mission.output.put(deepcopy(message))
            self.logger.info(message)
            return False

        return False

    def stop_mission(self):
        self.status.mission_in_progress = False
        message: StopMessage = StopMissionMessages.success()
        self.queues.stop_mission.output.put(deepcopy(message))
        self.logger.info(message)

    def log_state_transition(self, next_state):
        if next_state != self.status.current_state:
            self.transitions_list.append(next_state)


def main(injector: Injector):
    state_machine = injector.get(StateMachine)
    state_machine.begin()
