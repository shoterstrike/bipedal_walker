import math
import random

import numpy as np

from keras.models import Sequential
from keras.layers import Dense, Dropout
from keras.optimizers import SGD

import Box2D
from Box2D.b2 import edgeShape, circleShape, fixtureDef, polygonShape, revoluteJointDef, contactListener
import gym
from gym import spaces
from gym.utils import colorize, seeding

# This is simple 4-joints walker robot environment.
#
# There are two versions:
#
# - Normal, with slightly uneven terrain.
#
# - Hardcore with ladders, stumps, pitfalls.
#
# Reward is given for moving forward, total 300+ points up to the far end. If the robot falls,
# it gets -100. Applying motor torque costs a small amount of points, more optimal agent
# will get better score.
#
# Heuristic is provided for testing, it's also useful to get demonstrations to
# learn from. To run heuristic:
#
# python gym/envs/box2d/bipedal_walker.py
#
# State consists of hull angle speed, angular velocity, horizontal speed, vertical speed,
# position of joints and joints angular speed, legs contact with ground, and 10 lidar
# rangefinder measurements to help to deal with the hardcore version. There's no coordinates
# in the state vector. Lidar is less useful in normal version, but it works.
#
# To solve the game you need to get 300 points in 1600 time steps.
#
# To solve hardcore version you need 300 points in 2000 time steps.
#
# Created by Oleg Klimov. Licensed on the same terms as the rest of OpenAI Gym.
from keras.utils import to_categorical

FPS = 50
SCALE = 30.0  # affects how fast-paced the game is, forces should be adjusted as well

MOTORS_TORQUE = 80
SPEED_HIP = 4
SPEED_KNEE = 6
LIDAR_RANGE = 160 / SCALE

INITIAL_RANDOM = 5

HULL_POLY = [
    (-30, +9), (+6, +9), (+34, +1),
    (+34, -8), (-30, -8)
]
LEG_DOWN = -8 / SCALE
LEG_W, LEG_H = 8 / SCALE, 34 / SCALE

VIEWPORT_W = 600
VIEWPORT_H = 400

TERRAIN_STEP = 14 / SCALE
TERRAIN_LENGTH = 200  # in steps
TERRAIN_HEIGHT = VIEWPORT_H / SCALE / 4
TERRAIN_GRASS = 10  # low long are grass spots, in steps
TERRAIN_STARTPAD = 20  # in steps
FRICTION = 2.5


class ContactDetector(contactListener):
    def __init__(self, env):
        contactListener.__init__(self)
        self.env = env

    def BeginContact(self, contact):
        if self.env.hull == contact.fixtureA.body or self.env.hull == contact.fixtureB.body:
            self.env.game_over = True
        for leg in [self.env.legs[1], self.env.legs[3]]:
            if leg in [contact.fixtureA.body, contact.fixtureB.body]:
                leg.ground_contact = True

    def EndContact(self, contact):
        for leg in [self.env.legs[1], self.env.legs[3]]:
            if leg in [contact.fixtureA.body, contact.fixtureB.body]:
                leg.ground_contact = False


class BipedalWalker(gym.Env):
    metadata = {
        'render.modes': ['human', 'rgb_array'],
        'video.frames_per_second': FPS
    }

    hardcore = False

    def __init__(self):
        self._seed()
        self.viewer = None

        self.world = Box2D.b2World()
        self.terrain = None
        self.hull = None

        self.prev_shaping = None
        self._reset()

        high = np.array([np.inf] * 24)
        self.action_space = spaces.Box(np.array([-1, -1, -1, -1]), np.array([+1, +1, +1, +1]))
        self.observation_space = spaces.Box(-high, high)

    def _seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _destroy(self):
        if not self.terrain: return
        self.world.contactListener = None
        for t in self.terrain:
            self.world.DestroyBody(t)
        self.terrain = []
        self.world.DestroyBody(self.hull)
        self.hull = None
        for leg in self.legs:
            self.world.DestroyBody(leg)
        self.legs = []
        self.joints = []

    def _generate_terrain(self, hardcore):
        GRASS, STUMP, STAIRS, PIT, _STATES_ = range(5)
        state = GRASS
        velocity = 0.0
        y = TERRAIN_HEIGHT
        counter = TERRAIN_STARTPAD
        oneshot = False
        self.terrain = []
        self.terrain_x = []
        self.terrain_y = []
        for i in range(TERRAIN_LENGTH):
            x = i * TERRAIN_STEP
            self.terrain_x.append(x)

            if state == GRASS and not oneshot:
                velocity = 0.8 * velocity + 0.01 * np.sign(TERRAIN_HEIGHT - y)
                if i > TERRAIN_STARTPAD: velocity += self.np_random.uniform(-1, 1) / SCALE  # 1
                y += velocity

            elif state == PIT and oneshot:
                counter = self.np_random.randint(3, 5)
                poly = [
                    (x, y),
                    (x + TERRAIN_STEP, y),
                    (x + TERRAIN_STEP, y - 4 * TERRAIN_STEP),
                    (x, y - 4 * TERRAIN_STEP),
                ]
                t = self.world.CreateStaticBody(
                    fixtures=fixtureDef(
                        shape=polygonShape(vertices=poly),
                        friction=FRICTION
                    ))
                t.color1, t.color2 = (1, 1, 1), (0.6, 0.6, 0.6)
                self.terrain.append(t)
                t = self.world.CreateStaticBody(
                    fixtures=fixtureDef(
                        shape=polygonShape(vertices=[(p[0] + TERRAIN_STEP * counter, p[1]) for p in poly]),
                        friction=FRICTION
                    ))
                t.color1, t.color2 = (1, 1, 1), (0.6, 0.6, 0.6)
                self.terrain.append(t)
                counter += 2
                original_y = y

            elif state == PIT and not oneshot:
                y = original_y
                if counter > 1:
                    y -= 4 * TERRAIN_STEP

            elif state == STUMP and oneshot:
                counter = self.np_random.randint(1, 3)
                poly = [
                    (x, y),
                    (x + counter * TERRAIN_STEP, y),
                    (x + counter * TERRAIN_STEP, y + counter * TERRAIN_STEP),
                    (x, y + counter * TERRAIN_STEP),
                ]
                t = self.world.CreateStaticBody(
                    fixtures=fixtureDef(
                        shape=polygonShape(vertices=poly),
                        friction=FRICTION
                    ))
                t.color1, t.color2 = (1, 1, 1), (0.6, 0.6, 0.6)
                self.terrain.append(t)

            elif state == STAIRS and oneshot:
                stair_height = +1 if self.np_random.rand() > 0.5 else -1
                stair_width = self.np_random.randint(4, 5)
                stair_steps = self.np_random.randint(3, 5)
                original_y = y
                for s in range(stair_steps):
                    poly = [
                        (x + (s * stair_width) * TERRAIN_STEP, y + (s * stair_height) * TERRAIN_STEP),
                        (x + ((1 + s) * stair_width) * TERRAIN_STEP, y + (s * stair_height) * TERRAIN_STEP),
                        (x + ((1 + s) * stair_width) * TERRAIN_STEP, y + (-1 + s * stair_height) * TERRAIN_STEP),
                        (x + (s * stair_width) * TERRAIN_STEP, y + (-1 + s * stair_height) * TERRAIN_STEP),
                    ]
                    t = self.world.CreateStaticBody(
                        fixtures=fixtureDef(
                            shape=polygonShape(vertices=poly),
                            friction=FRICTION
                        ))
                    t.color1, t.color2 = (1, 1, 1), (0.6, 0.6, 0.6)
                    self.terrain.append(t)
                counter = stair_steps * stair_width

            elif state == STAIRS and not oneshot:
                s = stair_steps * stair_width - counter - stair_height
                n = s / stair_width
                y = original_y + (n * stair_height) * TERRAIN_STEP

            oneshot = False
            self.terrain_y.append(y)
            counter -= 1
            if counter == 0:
                counter = self.np_random.randint(TERRAIN_GRASS / 2, TERRAIN_GRASS)
                if state == GRASS and hardcore:
                    state = self.np_random.randint(1, _STATES_)
                    oneshot = True
                else:
                    state = GRASS
                    oneshot = True

        self.terrain_poly = []
        for i in range(TERRAIN_LENGTH - 1):
            poly = [
                (self.terrain_x[i], self.terrain_y[i]),
                (self.terrain_x[i + 1], self.terrain_y[i + 1])
            ]
            t = self.world.CreateStaticBody(
                fixtures=fixtureDef(
                    shape=edgeShape(vertices=poly),
                    friction=FRICTION,
                    categoryBits=0x0001,
                ))
            color = (0.3, 1.0 if i % 2 == 0 else 0.8, 0.3)
            t.color1 = color
            t.color2 = color
            self.terrain.append(t)
            color = (0.4, 0.6, 0.3)
            poly += [(poly[1][0], 0), (poly[0][0], 0)]
            self.terrain_poly.append((poly, color))
        self.terrain.reverse()

    def _generate_clouds(self):
        # Sorry for the clouds, couldn't resist
        self.cloud_poly = []
        for i in range(TERRAIN_LENGTH // 20):
            x = self.np_random.uniform(0, TERRAIN_LENGTH) * TERRAIN_STEP
            y = VIEWPORT_H / SCALE * 3 / 4
            poly = [
                (x + 15 * TERRAIN_STEP * math.sin(3.14 * 2 * a / 5) + self.np_random.uniform(0, 5 * TERRAIN_STEP),
                 y + 5 * TERRAIN_STEP * math.cos(3.14 * 2 * a / 5) + self.np_random.uniform(0, 5 * TERRAIN_STEP))
                for a in range(5)]
            x1 = min([p[0] for p in poly])
            x2 = max([p[0] for p in poly])
            self.cloud_poly.append((poly, x1, x2))

    def _reset(self):
        self._destroy()
        self.world.contactListener_bug_workaround = ContactDetector(self)
        self.world.contactListener = self.world.contactListener_bug_workaround
        self.game_over = False
        self.prev_shaping = None
        self.scroll = 0.0
        self.lidar_render = 0

        W = VIEWPORT_W / SCALE
        H = VIEWPORT_H / SCALE

        self._generate_terrain(self.hardcore)
        self._generate_clouds()

        init_x = TERRAIN_STEP * TERRAIN_STARTPAD / 2
        init_y = TERRAIN_HEIGHT + 2 * LEG_H
        self.hull = self.world.CreateDynamicBody(
            position=(init_x, init_y),
            fixtures=fixtureDef(
                shape=polygonShape(vertices=[(x / SCALE, y / SCALE) for x, y in HULL_POLY]),
                density=5.0,
                friction=0.1,
                categoryBits=0x0020,
                maskBits=0x001,  # collide only with ground
                restitution=0.0)  # 0.99 bouncy
        )
        self.hull.color1 = (0.5, 0.4, 0.9)
        self.hull.color2 = (0.3, 0.3, 0.5)
        self.hull.ApplyForceToCenter((self.np_random.uniform(-INITIAL_RANDOM, INITIAL_RANDOM), 0), True)

        self.legs = []
        self.joints = []
        for i in [-1, +1]:
            leg = self.world.CreateDynamicBody(
                position=(init_x, init_y - LEG_H / 2 - LEG_DOWN),
                angle=(i * 0.05),
                fixtures=fixtureDef(
                    shape=polygonShape(box=(LEG_W / 2, LEG_H / 2)),
                    density=1.0,
                    restitution=0.0,
                    categoryBits=0x0020,
                    maskBits=0x001)
            )
            leg.color1 = (0.6 - i / 10., 0.3 - i / 10., 0.5 - i / 10.)
            leg.color2 = (0.4 - i / 10., 0.2 - i / 10., 0.3 - i / 10.)
            rjd = revoluteJointDef(
                bodyA=self.hull,
                bodyB=leg,
                localAnchorA=(0, LEG_DOWN),
                localAnchorB=(0, LEG_H / 2),
                enableMotor=True,
                enableLimit=True,
                maxMotorTorque=MOTORS_TORQUE,
                motorSpeed=i,
                lowerAngle=-0.8,
                upperAngle=1.1,
            )
            self.legs.append(leg)
            self.joints.append(self.world.CreateJoint(rjd))

            lower = self.world.CreateDynamicBody(
                position=(init_x, init_y - LEG_H * 3 / 2 - LEG_DOWN),
                angle=(i * 0.05),
                fixtures=fixtureDef(
                    shape=polygonShape(box=(0.8 * LEG_W / 2, LEG_H / 2)),
                    density=1.0,
                    restitution=0.0,
                    categoryBits=0x0020,
                    maskBits=0x001)
            )
            lower.color1 = (0.6 - i / 10., 0.3 - i / 10., 0.5 - i / 10.)
            lower.color2 = (0.4 - i / 10., 0.2 - i / 10., 0.3 - i / 10.)
            rjd = revoluteJointDef(
                bodyA=leg,
                bodyB=lower,
                localAnchorA=(0, -LEG_H / 2),
                localAnchorB=(0, LEG_H / 2),
                enableMotor=True,
                enableLimit=True,
                maxMotorTorque=MOTORS_TORQUE,
                motorSpeed=1,
                lowerAngle=-1.6,
                upperAngle=-0.1,
            )
            lower.ground_contact = False
            self.legs.append(lower)
            self.joints.append(self.world.CreateJoint(rjd))

        self.drawlist = self.terrain + self.legs + [self.hull]

        class LidarCallback(Box2D.b2.rayCastCallback):
            def ReportFixture(self, fixture, point, normal, fraction):
                if (fixture.filterData.categoryBits & 1) == 0:
                    return 1
                self.p2 = point
                self.fraction = fraction
                return 0

        self.lidar = [LidarCallback() for _ in range(10)]

        return self._step(np.array([0, 0, 0, 0]))[0]

    def _step(self, action):
        # self.hull.ApplyForceToCenter((0, 20), True) -- Uncomment this to receive a bit of stability help
        control_speed = False  # Should be easier as well
        if control_speed:
            self.joints[0].motorSpeed = float(SPEED_HIP * np.clip(action[0], -1, 1))
            self.joints[1].motorSpeed = float(SPEED_KNEE * np.clip(action[1], -1, 1))
            self.joints[2].motorSpeed = float(SPEED_HIP * np.clip(action[2], -1, 1))
            self.joints[3].motorSpeed = float(SPEED_KNEE * np.clip(action[3], -1, 1))
        else:
            self.joints[0].motorSpeed = float(SPEED_HIP * np.sign(action[0]))
            self.joints[0].maxMotorTorque = float(MOTORS_TORQUE * np.clip(np.abs(action[0]), 0, 1))
            self.joints[1].motorSpeed = float(SPEED_KNEE * np.sign(action[1]))
            self.joints[1].maxMotorTorque = float(MOTORS_TORQUE * np.clip(np.abs(action[1]), 0, 1))
            self.joints[2].motorSpeed = float(SPEED_HIP * np.sign(action[2]))
            self.joints[2].maxMotorTorque = float(MOTORS_TORQUE * np.clip(np.abs(action[2]), 0, 1))
            self.joints[3].motorSpeed = float(SPEED_KNEE * np.sign(action[3]))
            self.joints[3].maxMotorTorque = float(MOTORS_TORQUE * np.clip(np.abs(action[3]), 0, 1))

        self.world.Step(1.0 / FPS, 6 * 30, 2 * 30)

        pos = self.hull.position
        vel = self.hull.linearVelocity

        for i in range(10):
            self.lidar[i].fraction = 1.0
            self.lidar[i].p1 = pos
            self.lidar[i].p2 = (
                pos[0] + math.sin(1.5 * i / 10.0) * LIDAR_RANGE,
                pos[1] - math.cos(1.5 * i / 10.0) * LIDAR_RANGE)
            self.world.RayCast(self.lidar[i], self.lidar[i].p1, self.lidar[i].p2)

        state = [
            self.hull.angle,  # Normal angles up to 0.5 here, but sure more is possible.
            2.0 * self.hull.angularVelocity / FPS,
            0.3 * vel.x * (VIEWPORT_W / SCALE) / FPS,  # Normalized to get -1..1 range
            0.3 * vel.y * (VIEWPORT_H / SCALE) / FPS,
            self.joints[0].angle,
            # This will give 1.1 on high up, but it's still OK (and there should be spikes on hiting the ground, that's normal too)
            self.joints[0].speed / SPEED_HIP,
            self.joints[1].angle + 1.0,
            self.joints[1].speed / SPEED_KNEE,
            1.0 if self.legs[1].ground_contact else 0.0,
            self.joints[2].angle,
            self.joints[2].speed / SPEED_HIP,
            self.joints[3].angle + 1.0,
            self.joints[3].speed / SPEED_KNEE,
            1.0 if self.legs[3].ground_contact else 0.0
        ]
        state += [l.fraction for l in self.lidar]
        assert len(state) == 24

        self.scroll = pos.x - VIEWPORT_W / SCALE / 5

        shaping = 130 * pos[
            0] / SCALE  # moving forward is a way to receive reward (normalized to get 300 on completion)
        shaping -= 5.0 * abs(state[0])  # keep head straight, other than that and falling, any behavior is unpunished

        reward = 0
        if self.prev_shaping is not None:
            reward = shaping - self.prev_shaping
        self.prev_shaping = shaping

        for a in action:
            reward -= 0.00035 * MOTORS_TORQUE * np.clip(np.abs(a), 0, 1)
            # normalized to about -50.0 using heuristic, more optimal agent should spend less

        done = False
        if self.game_over or pos[0] < 0:
            reward = -100
            done = True
        if pos[0] > (TERRAIN_LENGTH - TERRAIN_GRASS) * TERRAIN_STEP:
            done = True
        return np.array(state), reward, done, {}

    def _render(self, mode='human', close=False):
        if close:
            if self.viewer is not None:
                self.viewer.close()
                self.viewer = None
            return

        from gym.envs.classic_control import rendering
        if self.viewer is None:
            self.viewer = rendering.Viewer(VIEWPORT_W, VIEWPORT_H)
        self.viewer.set_bounds(self.scroll, VIEWPORT_W / SCALE + self.scroll, 0, VIEWPORT_H / SCALE)

        self.viewer.draw_polygon([
            (self.scroll, 0),
            (self.scroll + VIEWPORT_W / SCALE, 0),
            (self.scroll + VIEWPORT_W / SCALE, VIEWPORT_H / SCALE),
            (self.scroll, VIEWPORT_H / SCALE),
        ], color=(0.9, 0.9, 1.0))
        for poly, x1, x2 in self.cloud_poly:
            if x2 < self.scroll / 2: continue
            if x1 > self.scroll / 2 + VIEWPORT_W / SCALE: continue
            self.viewer.draw_polygon([(p[0] + self.scroll / 2, p[1]) for p in poly], color=(1, 1, 1))
        for poly, color in self.terrain_poly:
            if poly[1][0] < self.scroll: continue
            if poly[0][0] > self.scroll + VIEWPORT_W / SCALE: continue
            self.viewer.draw_polygon(poly, color=color)

        self.lidar_render = (self.lidar_render + 1) % 100
        i = self.lidar_render
        if i < 2 * len(self.lidar):
            l = self.lidar[i] if i < len(self.lidar) else self.lidar[len(self.lidar) - i - 1]
            self.viewer.draw_polyline([l.p1, l.p2], color=(1, 0, 0), linewidth=1)

        for obj in self.drawlist:
            for f in obj.fixtures:
                trans = f.body.transform
                if type(f.shape) is circleShape:
                    t = rendering.Transform(translation=trans * f.shape.pos)
                    self.viewer.draw_circle(f.shape.radius, 30, color=obj.color1).add_attr(t)
                    self.viewer.draw_circle(f.shape.radius, 30, color=obj.color2, filled=False, linewidth=2).add_attr(t)
                else:
                    path = [trans * v for v in f.shape.vertices]
                    self.viewer.draw_polygon(path, color=obj.color1)
                    path.append(path[0])
                    self.viewer.draw_polyline(path, color=obj.color2, linewidth=2)

        flagy1 = TERRAIN_HEIGHT
        flagy2 = flagy1 + 50 / SCALE
        x = TERRAIN_STEP * 3
        self.viewer.draw_polyline([(x, flagy1), (x, flagy2)], color=(0, 0, 0), linewidth=2)
        f = [(x, flagy2), (x, flagy2 - 10 / SCALE), (x + 25 / SCALE, flagy2 - 5 / SCALE)]
        self.viewer.draw_polygon(f, color=(0.9, 0.2, 0))
        self.viewer.draw_polyline(f + [f[0]], color=(0, 0, 0), linewidth=2)

        return self.viewer.render(return_rgb_array=mode == 'rgb_array')


class BipedalWalkerHardcore(BipedalWalker):
    hardcore = True


def get_power(a):
    if a == -0.1:
        return 1
    if a == 0:
        return 0
    return 2


if __name__ == "__main__":
    # #site-ul sursa: https://www.tensorflow.org/get_started/mnist/pros
    #
    # #added some code to evaluate the way we will implement the neuronal network
    # #we have to decide how and in which way will we implement it
    # sess = tf.InteractiveSession()
    # W = tf.Variable(tf.zeros([24, 4]))
    # #s are 24 de campuri si ar trebui sa aiba 4 output-uri
    # b = tf.Variable(tf.zeros([4]))
    # y = tf.nn.softmax(tf.matmul(x, W) + b)#pentru iteratii
    # sess.run(tf.global_variables_initializer())
    #
    # #cross_entropy = tf.reduce_mean(-tf.reduce_sum(y_ * tf.log(y), reduction_indices=[1]))
    # cross_entropy = tf.reduce_mean(
    #     tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=y))
    # train_step = tf.train.AdamOptimizer(1e-4).minimize(cross_entropy)
    #
    # #iteratii
    # train_step = tf.train.GradientDescentOptimizer(0.5).minimize(cross_entropy)
    # correct_prediction = tf.equal(tf.argmax(y, 1), tf.argmax(y_, 1)) #aici trebuie implementata functia de activare
    # accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))

    env = BipedalWalker()
    env.reset()
    steps = 0

    random.seed(12345)
    model = Sequential()
    model.add(Dense(0.2, input_shape=(24,)))
    model.add(Dense(30, input_shape=(24,), activation='sigmoid'))
    model.add(Dense(81, activation='softmax'))

    optimizer = SGD(lr=0.05, momentum=0.8)

    model.compile(optimizer=optimizer, loss='categorical_crossentropy',
                  metrics=['accuracy'])

    last_state = np.zeros(24)
    last_move_index = 0
    discount_factor = 0.5
    possible_moves = [-0.1, 0, 0.1]

    # functie de clip pentru valori -1 -> 1 (sau -0.1 -> 0.1)
    # functie de explorare, ca sa nu ia chiar intotdeauna cea mai buna solutie
    # acesta functie de explorare sa aiba o probabilitate mai mica cu cat au trecut mai multe iteratii
    # dupa ce a invatat destul aplicam algoritmul de Q-learning care invata si updateaza ultimii 100 de pasi pe formula
    #                                           scrisa pe caiet
    # alte chestii de care am uitat, in principiu optimizari presupun
    for i in range(0, 100):
        total_reward = 0
        env.reset()
        a = np.array([0.0, 0.0, 0.0, 0.0])
        steps = 0
        last_100_steps = []
        while True:
            current_state, current_reward, done, info = env.step(a)
            old_state = np.array([last_state])
            old_state_adjust_value = np.array([np.zeros(81)])
            old_state_adjust_value[0][last_move_index] = total_reward

            model.train_on_batch(old_state, old_state_adjust_value)

            total_reward += current_reward
            if steps % 100 == 0:
                print("Current Reward" + str(current_reward))
                print("Total Reward"+str(total_reward))
            steps += 1
            if steps == 100:
                done = True
                current_reward = -1000

            best_score = -2018
            best_index = 0
            state = list(current_state)

            random_move = random.randrange(0, 100)
            if random_move > 10:
                score = model.predict(np.array([state]), batch_size=1)
                for x in range(0, 81):
                    if score[0][x] > best_score:
                        best_score = score[0][x]
                        best_index = x
            else:
                random_move = random.randrange(0, 81)
                best_index = random_move

            a = np.array([a[0], a[1], a[2], a[3]])
            move_index = best_index
            for i in reversed(range(0, 4)):
                power = pow(3, i)
                aux = move_index // power
                a[i] += possible_moves[aux]

                if a[i] > 1:
                    a[i] = 1
                if a[i] < -1:
                    a[i] = -1

                move_index = move_index - power * (move_index // power)

            last_move_index = best_index
            last_state = state

            last_100_steps.append((last_state, last_move_index))

            env.render()
            if done:
                decay = len(last_100_steps)
                for i in reversed(range(0, len(last_100_steps))):
                    step_state = last_100_steps[i][0]
                    step_index = last_100_steps[i][1]

                    old_state = np.array([step_state])
                    old_state_adjust_value = np.array([np.zeros(81)])
                    old_state_adjust_value[0][step_index] = current_reward * (decay / len(last_100_steps)/100)

                    model.train_on_batch(old_state, old_state_adjust_value)
                    decay -= 1
                break
